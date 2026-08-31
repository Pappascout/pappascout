"""Raportin näkymämalli: **mitä** raportissa sanotaan.

Moduuli lukee :class:`~pappascout.domain.report.Report`in ja rakentaa siitä
rivejä ja väitteitä. Se **ei laske mitään**: jokainen luku poimitaan mallista
sellaisenaan, eikä täällä ole yhtään yhteenlaskua, keskiarvoa eikä osamäärää.
Ainoa "logiikka" on valinta -- mitkä havainnot ansaitsevat rivin -- ja
muotoilu.

Säännöt, jotka näkyvät jokaisessa funktiossa
--------------------------------------------
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

**Runko puhuu nimillä, ja tunnisteilla on oma lukunsa.** Joukkueen ja
kokoonpanojen tiivisteet, pelaajien SteamID64 ja karttojen demotunnisteet
eivät ole rungossa vaan luvussa :data:`TRACEABILITY_HEADING`. Sääntö on
täällä eikä vain funktioiden sisällä, koska se koskee viittä niistä
(:func:`_title`, :func:`_team_text`, :func:`_roster_text`, :func:`_summary`,
:func:`_traceability`) -- yhden sisällä kirjoitettuna se ei kertoisi, että
poikkeuksia on tarkalleen kolme:

1. **Kierrosliitteen polku.** Polku on käyttökelpoinen vain sellaisenaan, ja
   se on lukemisen apu eikä jäljitettävyysmerkintä.
2. **Puuttuvan demon rivi.** Tunniste on osa komentoa, jonka lukija kopioi
   (``uv run pappascout parse <demo>``); ilman sitä rivi ei kertoisi mitä
   tehdä.
3. **Kartta, jonka nimeä ei tunnistettu.** Silloin ``map_name`` *on*
   ``map_demo_id`` (ks. :class:`~pappascout.domain.report.MapReport`), eli
   tunniste on kartan ainoa nimi -- vaihtoehto olisi nimetön karttaluku.

Poikkeukset sanotaan lukuohjeessa ääneen. Ilman sitä raportti väittäisi
itsestään enemmän kuin on totta, ja juuri se on tässä tiedostossa se virhe,
joka toistuu: teksti lupaa ehdottomuuden, jota koodi ei pidä.

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
    ArmoredPlayers,
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
    "TRACEABILITY_HEADING",
    "UNNAMED_PLAYER",
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

#: Merkintä alueelle, joka on **arvio** eikä havainto: räjähdyksen alue on
#: luettu demon pistepilven lähimmästä ruudusta. Ilman merkintää raportti
#: esittäisi arvion havaintona.
#:
#: Merkintä säilyy, vaikka menetelmä vaihtui Story 2.9:ssä lähimmästä
#: pelaajasta pistepilveen. Alue on yhä johdos: se on *pelin oma* aluenimi
#: siitä kohdasta, jossa joku on seissyt lähinnä räjähdystä -- ei
#: räjähdyspaikan oma nimi, koska sellaista ei ole olemassa. Tarkempi arvio on
#: yhä arvio.
ESTIMATE_MARK = " (arvio)"

#: Jäljitettävyysluvun otsikko **sellaisena kuin malli sen latoo**.
#:
#: Nimi on täällä, koska raportin oma teksti viittaa siihen kahdesta paikasta:
#: yhteenvedon nimetön joukkue kertoo mistä tunniste löytyy, ja lukuohje
#: kertoo saman kaikista tunnisteista. Otsikkorivin ``## `` omistaa malli --
#: rakenne on mallin asia -- joten nimi esiintyy kahdessa tiedostossa, ja
#: testi vartioi että ne ovat samat. Ilman vartijaa luvun uudelleennimeäminen
#: jättäisi raporttiin kaksi viittausta lukuun, jota ei ole.
#:
#: **Lukua ei kutsuta liitteeksi.** Raportissa on jo ``Kierrosliite``, joka on
#: eri asia: se osoittaa ``classify``-vaiheen kierroslistoihin arkistossa,
#: kun tämä luku on raportin sisällä. Kaksi eri asiaa samalla sanalla tekee
#: kumman tahansa mainitsemisen epäselväksi, joten luvusta puhutaan sen omalla
#: nimellä sekä koodissa, testeissä että READMEssa.
TRACEABILITY_HEADING = "Tekninen jäljitettävyys"


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
    #: Jäljitettävyysluvun rivit: tunnisteet, jotka eivät ole rungossa.
    #:
    #: Oletus on tyhjä vain siksi, että kenttä on lisätty olemassa olevaan
    #: luokkaan; :func:`build_view` täyttää sen **aina**, koska joukkueella on
    #: tunniste myös tyhjässä raportissa. Malli latoo luvun ehdoitta kuten
    #: ``Kierrosliite``n ja ``Lukuohje``n, joten tyhjä jono tuottaisi paljaan
    #: otsikon -- tilanne, jota ei ole olemassa eikä siksi vartioida.
    traceability: tuple[SummaryItem, ...] = ()
    #: Jäljitettävyysluvun selitys: miksi tunnisteet ovat siellä eivätkä
    #: rungossa.
    traceability_note: str = ""
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
    armored_shown: bool = False
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
    return _threshold_int(report, "small_sample_rounds")


def _threshold_int(report: Report, name: str) -> int | None:
    """Yksi positiivinen kokonaislukukynnys ``report.json``ista.

    Erotettu :func:`pattern_min_rounds`ista, koska kaksi eri riviä tarvitsee
    saman säännön: arvo luetaan **raportista eikä asetuksista**, ja jos sitä
    ei ole, rivi kirjoitetaan ilman kynnystä sen sijaan että renderöinti
    keksisi oman luvun. Kahtena kopiona toinen erkaantuisi.

    ``bool`` hylätään erikseen, koska Pythonissa se on ``int``: ``True``
    latoisi kynnykseksi luvun 1.
    """
    section = report.thresholds_used.get("thresholds")
    if not isinstance(section, Mapping):
        return None
    value = section.get(name)
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
        if use.area_source == "point_cloud":
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


def _player_count_line(
    label: str,
    bars: Sequence[tuple[int, int]],
    m: int,
    rounds_unknown: int,
    min_n: int,
    flags: _Flags,
) -> Line | None:
    """Yhden pelaajalaskurin jakauma yhtenä rivinä.

    Jaettu aseistettujen ja panssaroitujen kesken samasta syystä kuin
    ``stages.parse``in ``_column_distribution``: ne ovat eri havaintoja samasta
    tickistä, ja kaksi kopiota latoisi ne ennen pitkää eri tavalla -- toinen
    suodattaisi kynnyksellä ja toinen ei, tai toinen kertoisi puuttuvista
    havainnoista ja toinen vaikenisi. Rivien **ero** on tämän raportin
    havainto, joten niiden muodon on pysyttävä samana.

    Args:
        label: Rivin otsikko.
        bars: ``(pelaajamäärä, kierroksia)`` -parit, järjestämättöminä.
        m: Kierrokset, joilta havainto saatiin -- väitteiden nimittäjä.
        rounds_unknown: Kierrokset, joilta havaintoa ei saatu.
        min_n: Toistumisen kynnys; alle jäävät pylväät pudotetaan ja
            lasketaan ``flags.dropped``iin, kun kynnys on yli yhden.
        flags: Raportin laajuinen kerääjä.

    Returns:
        Rivi, tai ``None`` jos kerrottavaa ei ole. **Pelkkä huomautus
        riittää** riviksi: "havainto puuttuu 3 kierrokselta" on eri asia kuin
        "kukaan ei kantanut panssaria", ja ilman riviä lukija ei erottaisi
        niitä -- jälkimmäinen näkyisi nollana ja edellinen ei mitenkään.
    """
    claims: list[Claim] = []
    for value, n in sorted(bars, key=lambda bar: (-bar[1], -bar[0])):
        if n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        claims.append(Claim(text=str(value), n=n, m=m))
    note = None
    if rounds_unknown:
        note = f"havainto puuttuu {rounds_unknown} kierrokselta"
    if not claims and note is None:
        return None
    return Line(label=label, claims=tuple(claims), note=note)


def _armed_line(armed: ArmedPlayers, min_n: int, flags: _Flags) -> Line | None:
    """Aseistettujen pelaajien jakauma ostoajan lopussa.

    Lippu nostetaan aina kun rivi kirjoitetaan -- myös silloin kun rivillä on
    pelkkä huomautus. Muuten lukija näkisi otsikon "aseistettuja" ilman sen
    määritelmää, ja määritelmä on juuri se, mikä erottaa rivin
    panssarirvistä.
    """
    line = _player_count_line(
        "aseistettuja ostoajan lopussa",
        [(bar.armed, bar.n) for bar in armed.counts],
        armed.m,
        armed.rounds_unknown,
        min_n,
        flags,
    )
    if line is not None:
        flags.armed_shown = True
    return line


def _armored_line(
    armored: ArmoredPlayers, min_n: int, flags: _Flags
) -> Line | None:
    """Panssaroitujen pelaajien jakauma ostoajan lopussa.

    Oma rivinsä aseistettujen rivin vieressä, ei sen tilalla. Tästä luetaan
    tavoiteanalyysin *"5 kevlaria"* ja *"ei kevuja"*, joita aseistettujen
    riviltä ei voi lukea: pistoolikierroksella aseistettuja on käytännössä 0,
    koska 800 dollarilla ei osta sekä kevlaria että parannettua asetta. Kaksi
    riviä peräkkäin siis, ja juuri niiden ero on havainto.
    """
    line = _player_count_line(
        "panssaroituja ostoajan lopussa",
        [(bar.armored, bar.n) for bar in armored.counts],
        armored.m,
        armored.rounds_unknown,
        min_n,
        flags,
    )
    if line is not None:
        flags.armored_shown = True
    return line


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

    # Panssaririvi heti aseistettujen perässä: niiden ero on itse havainto,
    # eikä sitä näe, jos rivien välissä on muuta.
    armored = _armored_line(report_type.players_armored, min_n, flags)
    if armored is not None:
        lines.append(armored)

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
    """Yhteenvedon rivit. Jokainen kohta, joka voisi kadota, on täällä.

    **Runko puhuu nimillä** (Story 2.12). Yhteenveto on se osa raporttia, jonka
    lukija näkee ensimmäisenä ottelua edeltävässä kiireessä, eikä se voi puhua
    tiivisteistä: joukkueen tunniste, kokoonpanotunnisteet ja pelaajien
    SteamID64:t ovat luvussa :data:`TRACEABILITY_HEADING`. Siirto eikä pudotus
    -- jokainen tunniste on yhä raportissa, vain eri paikassa.

    **Kynnykset eivät ole tunnisteita eivätkä siirry.** Kynnys kertoo *miten*
    luku laskettiin, joten lukija tarvitsee sitä väitteen arvioimiseen; sama
    koskee työkaluversioita ja aikaleimaa. Tunniste ei muuta yhtäkään raportin
    lukua -- se palvelee vain jäljittämistä. Juuri se ero ratkaisee, mikä rivi
    kuuluu yhteenvetoon.
    """
    team = report.team
    items = [SummaryItem("Joukkue", _team_text(team))]
    if len(team.lineup_keys) > 1:
        items.append(SummaryItem("Kokoonpanot", _lineups_text(team, report)))
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
                f"{len(team.roster)} pelaajaa ({roster_source}): "
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


def _lineups_text(team: Any, report: Report) -> str:
    """Rungon kokoonpanorivi: montako kokoonpanoa liitettiin ja millä ehdolla.

    Kolme lukua eikä yksi, koska yksi ei ole tarkistettavissa. ``lineup_keys``
    sisältää **kohteen oman kokoonpanon** (ks.
    :func:`~pappascout.domain.aggregate.lineups_of_same_team`, "``target``
    aina mukana"), joten pelkkä ``len`` lukisi kuin liitettyjä olisi yksi
    enemmän kuin oli. Rivi kertoo siis liitettyjen määrän *ja* kokonaismäärän,
    ja niiden summa on tarkistettavissa jäljitettävyysluvun tunnisteita
    laskemalla.

    Kynnys (``[thresholds].team_identity_min_common``, AD-6) luetaan
    raportista samalla säännöllä kuin pienen otannan raja, ja se kirjoitetaan
    riville kuten naapuririvillä ("alle 3 kierrosta merkitään"). Ilman sitä
    rivi väittäisi päätöksen ilman perustetta. Jos arvoa ei ole, rivi kertoo
    perusteen sanoina eikä keksi lukua.
    """
    total = len(team.lineup_keys)
    joined = total - 1
    count = "1 muu kokoonpano" if joined == 1 else f"{joined} muuta kokoonpanoa"
    min_common = _threshold_int(report, "team_identity_min_common")
    if min_common is None:
        rule = "yhteisten pelaajien perusteella"
    else:
        rule = f"vähintään {min_common} yhteisen pelaajan perusteella"
    return (
        f"{count} liitetty samaksi joukkueeksi {rule}; yhteensä {total} "
        f"kokoonpanoa, tunnisteet luvussa {TRACEABILITY_HEADING}"
    )


def _team_text(team: Any) -> str:
    """Joukkueen nimi -- tai rehellinen toteamus siitä, ettei sitä ole.

    Nimi on **havainto**: se on demon ``team_clan_name`` sellaisena kuin
    kokoonpanotaulu sen kirjasi. Ilman havaintoa tiivisteen toistaminen nimen
    paikalla väittäisi, että ``9ac92660986558d3`` on joukkueen nimi -- ja juuri
    sen takia raportti sanoo puuttumisen ääneen eikä keksi korviketta.

    **Sama peruste kantaa tunnisteen omaan lukuunsa** (Story 2.12). Jos
    tiiviste ei kelpaa nimen paikalle, se ei kelpaa myöskään nimen perään
    sulkeisiin rivillä, jonka lukija lukee ensimmäisenä: rivi kertoisi silloin
    kaksi asiaa, joista toinen ei ole hänelle mitään. Tunniste on luvussa
    :data:`TRACEABILITY_HEADING`, jossa se on tunniste eikä nimi -- ja
    nimettömän joukkueen rivi sanoo sen ääneen, koska siltä lukijalta
    tunniste on ainoa, mitä joukkueesta on.
    """
    if _has_name(team):
        return markdown_text(team.display_name)
    return (
        "nimi ei ole tiedossa. Demoista ei löytynyt joukkueelle klaaninimeä "
        "(team_clan_name), eikä raportti keksi nimeä muusta lähteestä; "
        f"tunniste on luvussa {TRACEABILITY_HEADING}."
    )


def _has_name(team: Any) -> bool:
    """Onko joukkueen nimi havainto vai tunniste sen paikalla.

    Lähde ratkaisee eikä vertailu tunnisteeseen: joukkue voisi olla nimeltään
    täsmälleen tunnisteensa näköinen, ja silloin vertailu väittäisi havaintoa
    puuttuvaksi.
    """
    return team.display_name_source == "clan_name"


#: Rosterin pelaaja, jonka nimeä ei saatu luettua.
#:
#: Paikanpitäjä eikä pois jättö: sen ansiosta rungon nimilista on täsmälleen
#: yhtä pitkä kuin ``roster``, joten rivin oma lukumäärä ja lista eivät voi
#: olla eri mieltä.
#:
#: Sama teksti on jäljitettävyysluvun rivin nimiönä, mutta **järjestysluvun
#: kanssa** (``2. nimi ei luettavissa``). Ilman lukua kaksi nimetöntä pelaajaa
#: -- tai kaksi samannimistä, mikä on CS2:ssa tavallista -- tuottaisi kaksi
#: identtistä nimiötä, eikä lukija voisi sanoa kumpi SteamID64 on kumman.
#: Luku on paikka rungon nimilistassa, joten pari löytyy laskemalla eikä
#: arvaamalla.
UNNAMED_PLAYER = "nimi ei luettavissa"


def _roster_text(entry: Any) -> str:
    """Yksi rosterirvi rungossa: **pelkkä nimi**.

    Story 2.6 päätti "molemmat, aina": nimi luettavuutta varten ja SteamID64
    sen rinnalla, koska tunniste on ainoa jäljitettävä arvo. Peruste ei ole
    muuttunut vääräksi -- tunniste on yhä ainoa arvo, joka ei vaihdu ottelusta
    toiseen -- mutta **paikka on** (Story 2.12). Seitsemän 17-numeroista lukua
    nimien rinnalla tekee rungon rivistä luettelon, jota ihminen ei lue
    ottelua edeltävässä kiireessä, eikä hän tarvitse sitä siellä. Pari
    nimi -> SteamID64 on kokonaisena luvussa :data:`TRACEABILITY_HEADING`.

    Paluuarvo on **sama merkkijono** kuin jäljitettävyysluvun nimiössä
    järjestysluvun jälkeen. Yhteinen lähde tekee järjestyslupauksesta
    tarkistettavan: jos nimi kirjoitettaisiin kahdesti, kaksi kirjoitusasua
    voisivat erkaantua eikä lukija enää löytäisi pariaan.

    Nimi on demon antama merkkijono, joten se kulkee :func:`markdown_text`in
    läpi. Nimen puuttuminen sanotaan ääneen eikä pelaajaa pudoteta: hän on
    mukana rosterin lukumäärässä, ja hänen SteamID64:nsä on luvussa
    :data:`TRACEABILITY_HEADING`.
    """
    if entry.display_name:
        return markdown_text(entry.display_name)
    return UNNAMED_PLAYER


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


# -- Tekninen jäljitettävyys -----------------------------------------------------


def _identifier(value: str) -> str:
    """Tunniste **kirjaimellisena**: koodijaksona eikä paettuna tekstinä.

    Jäljitettävyysluvun koko arvo on se, että tunnisteen voi kopioida
    raportista komennolle tai hakukoneelle. :func:`markdown_text` suojaisi
    Markdownin rakennemerkit mutta tekisi samalla arvosta eri merkkijonon:
    demotunniste ``ANCIENT_vs_RCAVE_VETERANS`` saisi kenoviivan jokaisen
    alaviivansa eteen eikä täsmäisi enää yhteenkään arkiston hakemistoon.
    Koodijakso säilyttää arvon tavu tavulta ja estää Markdownin tulkinnan
    samalla kerralla -- sama valinta kuin kierrosliitteen poluilla.

    Gravis on ainoa merkki, jota koodijakso ei voi sisältää, ja se on
    Windowsissa laillinen tiedostonimessä eli mahdollinen demotunnisteessa.
    Silloin arvo paetaan tekstinä: rikkinäinen koodijakso latoisi loppuraportin
    väärin, mikä on pahempi kuin kopioitavuuden menetys yhdellä rivillä.
    """
    if "`" in value:
        return markdown_text(value)
    return f"`{value}`"


def _traceability(report: Report) -> list[SummaryItem]:
    """Jäljitettävyysluvun rivit: jokainen tunniste, jolla runko ei puhu.

    Luku on **siirron toinen pää**. Runko puhuu nimillä (Story 2.12), ja jotta
    poistaminen olisi siirto eikä pudotus, jokaisen rungosta poistuneen
    tunnisteen on oltava täällä: joukkueen tiiviste, kokoonpanotiivisteet,
    pelaajien SteamID64 ja karttojen demotunnisteet.

    Rosterin rivit ovat pari **järjestysluku + nimi -> SteamID64** eivätkä yksi
    luettelo, koska juuri se pari on kysymys, johon luku vastaa: "kuka
    ``76561190000000001`` oikeasti on" on vastattavissa raportista itsestään
    ilman ``report.json``ia (tunniste on esimerkissä keksitty; oikeaa ei
    kirjoiteta lähdekoodiin). Järjestysluku on paikka ``roster``issa eli sama
    kuin rungon nimilistassa, ja se on rivillä siksi, että nimi **ei ole
    yksikäsitteinen avain**: kaksi nimetöntä tai kaksi samannimistä pelaajaa
    tuottaisivat ilman lukua kaksi identtistä nimiötä.

    Luku ei laske mitään eikä tuo yhtään uutta arvoa: kaikki neljä lähdettä
    ovat ``Report``issa valmiina (``team.key``, ``team.lineup_keys``,
    ``roster[].player_id``, ``maps[].map_demo_ids``). Juuri siksi uusi LUKU ei
    tällä kerralla tarkoita muutosta ``Report``iin.
    """
    team = report.team
    items = [SummaryItem("Joukkueen tunniste", _team_key_text(team))]
    # Sama ehto kuin rungon kokoonpanorivillä. Yhdellä kokoonpanolla oma rivi
    # toistaisi joukkueen tunnisteen sanasta sanaan: ``team.key`` **on**
    # kokoonpanotunniste (``lineups_of_same_team`` palauttaa kohteen aina
    # mukana), joten toisto ei olisi jäljitettävyyttä vaan kohinaa. Se ei
    # myöskään katoaisi -- joukkuerivi kantaa sen, ja sanoo sen ääneen.
    if len(team.lineup_keys) > 1:
        items.append(
            SummaryItem(
                "Kokoonpanotunnisteet",
                ", ".join(_identifier(key) for key in team.lineup_keys),
            )
        )
    for index, entry in enumerate(team.roster, start=1):
        items.append(
            SummaryItem(
                f"{index}. {_roster_text(entry)}", _identifier(entry.player_id)
            )
        )
    for index, map_report in enumerate(report.maps, start=1):
        items.append(
            SummaryItem(
                _map_label(index, map_report),
                ", ".join(
                    _identifier(demo_id) for demo_id in map_report.map_demo_ids
                ),
            )
        )
    return items


def _team_key_text(team: Any) -> str:
    """Joukkueen tunniste, ja yhden kokoonpanon tapauksessa mitä se myös on.

    ``team.key`` on ennen Epic 3:a kokoonpanotunniste, joten yhden
    kokoonpanon joukkueella se **on** se ainoa kokoonpanotunniste. Kaksi
    riviä samalla arvolla eri nimiöillä lukisi kuin arvoja olisi kaksi; yksi
    rivi, joka sanoo olevansa molempia, kertoo saman ilman toistoa.
    """
    if len(team.lineup_keys) == 1:
        return (
            f"{_identifier(team.key)} -- sama arvo kuin joukkueen ainoa "
            "kokoonpanotunniste"
        )
    return _identifier(team.key)


def _map_label(index: int, map_report: Any) -> str:
    """Karttarivin nimiö jäljitettävyysluvussa.

    **Nimi koodijaksona eikä paettuna tekstinä.** Kartan nimi on Story 2.11:n
    jälkeen havainto demon otsikosta, eikä sitä validoida karttapoolia vasten
    -- workshop-kartta nimeltä ``*|Aim|* Botz [beta]`` on laillinen havainto.
    Paljaana se katkaisisi rivin: nimiön lihavointi jäisi sulkeutumatta, ja
    juuri tämä rivi kantaa demotunnisteet eli koko luvun tarkoituksen.
    Escapetus taas tuottaisi kartalle toisen kirjoitusasun kuin karttaluvun
    otsikossa, ja raportti luetaan myös raakana. Koodijakso ei tee
    kumpaakaan: se säilyttää **täsmälleen samat merkit** ja estää silti
    Markdownin tulkinnan.

    Kun nimeä ei tunnistettu, ``map_name`` on ``map_demo_id`` itse (ks.
    :class:`~pappascout.domain.report.MapReport`). Silloin nimiö ja arvo
    olisivat sama merkkijono, eli rivi ei kertoisi mitään: nimiö sanoo sen
    sijaan mistä on kyse, ja järjestysluku kertoo minkä karttaluvun rivi
    koskee -- kaksi tunnistamatonta karttaa eivät saa saada samaa nimiötä.
    """
    if map_report.map_name_source == "unknown":
        return f"kartta {index}, nimeä ei tunnistettu"
    return _identifier(map_report.map_name)


#: Jäljitettävyysluvun selitys.
#:
#: Veeti 31.8.: *"ihmiselle hasheillä ja tunnisteilla ei ole mitään
#: merkitystä, mutta projektille ja sen toiminnalle ne ovat arvokkaita"*.
#: Molemmat asiat ovat totta samassa lauseessa, joten pudotus palvelisi vain
#: ensimmäistä. Luvun selitys sanoo tämän ääneen, jottei seuraava lukija
#: pidä lukua jäänteenä.
_TRACEABILITY_NOTE = (
    "Tunnisteet, jotka eivät ole rungossa: joukkueen ja kokoonpanojen "
    "tiivisteet, pelaajien SteamID64 ja karttojen demotunnisteet. Mitään ei "
    "ole poistettu -- ne ovat täällä, koska ne palvelevat vain jäljittämistä. "
    "Kynnykset, työkaluversiot ja aikaleima jäivät yhteenvetoon, koska ne "
    "kertovat miten luku laskettiin, eikä väitettä voi arvioida ilman niitä; "
    "tunniste ei muuta yhtäkään raportin lukua. Rungossa tunniste on vain "
    "siellä, missä se on ainoa käyttökelpoinen muoto: kierrosliitteen "
    "polussa, puuttuvan demon komennossa ja kartassa, jonka nimeä ei "
    "tunnistettu."
)


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
        # Merkintä kuuluu **vain** lähteelle ``unknown``. ``demo_header`` ja
        # ``map_demo_id`` ovat molemmat tunnistettuja nimiä: edellinen on
        # havainto demon otsikosta, jälkimmäinen päättely tunnisteesta.
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
        # Nimiö on demotunniste ja se **jää rungon riville**: syy sisältää
        # komennon, jonka lukija kopioi (``uv run pappascout parse <demo>``),
        # eikä komento toimi ilman tunnistetta. Koodijakso siksi, että
        # tunnisteen on kestettävä kopiointi -- sama peruste kuin
        # jäljitettävyysluvun arvoilla ja kierrosliitteen poluilla.
        missing_demos=tuple(
            SummaryItem(_identifier(entry.match), entry.reason)
            for entry in report.missing_demos
        ),
        maps=tuple(maps),
        legend=tuple(_legend(flags)),
        appendix_note=(
            _APPENDIX_NOTE if round_list_paths else _APPENDIX_NOTE_WITHOUT_PATHS
        ),
        appendix_paths=tuple(round_list_paths),
        traceability=tuple(_traceability(report)),
        traceability_note=_TRACEABILITY_NOTE,
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
            "alue on luettu demon pistepilvestä -- siitä kohdasta kartalla, "
            "jossa pelaajat ovat lähinnä räjähdystä oikeasti seisoneet."
        )
    notes.extend(_player_counter_legend(flags))
    if flags.kills_shown:
        notes.append(
            "Tapot alueittain: alue on **ampujan** oma alue tappohetkellä, ja "
            f"otanta (n/m {KILL_SAMPLE_UNIT}) laskee tappoja eikä kierroksia "
            "-- kierrostyypillä on yleensä enemmän tappoja kuin kierroksia."
        )
    notes.append(
        "Runko puhuu nimillä: joukkueen ja kokoonpanojen tiivisteet, "
        "pelaajien SteamID64 ja karttojen demotunnisteet ovat raportin "
        f"viimeisessä luvussa {TRACEABILITY_HEADING}. Kolme poikkeusta, "
        "joissa tunniste on rungossa siksi että se on siellä ainoa "
        "käyttökelpoinen muoto: kierrosliitteen polut, puuttuvan demon rivi "
        "(tunniste on osa komentoa, jonka voi kopioida) ja kartta, jonka "
        "nimeä ei tunnistettu (tunniste on kartan ainoa nimi)."
    )
    notes.append(
        "Raportti kuvaa vain havainnot. Tulkinta ja vastastrategia ovat lukijan."
    )
    return notes


def _player_counter_legend(flags: _Flags) -> list[str]:
    """Pelaajalaskureiden selitykset, yksi kappale kutakin näytettyä kohden.

    Kolme haaraa eikä kaksi riippumatonta lausetta. Kun molemmat rivit ovat
    raportissa, ne selitetään **yhtenä kappaleena**, koska luvut ovat
    sisäkkäisiä eivätkä rinnakkaisia: aseistetut ovat panssaroitujen
    osajoukko, molemmat luetaan samalta tickiltä ja samasta pelaajajoukosta,
    ja jakajat ovat samat. Kahtena erillisenä lauseena "aseistettuja 0" ja
    "panssaroituja 5" jäisivät kahdeksi irralliseksi luvuksi, ja niiden ero on
    juuri se, mitä rivit yhdessä kertovat.
    """
    nesting = (
        "Aseistettu = panssari JA parannettu ase ostoajan lopussa; "
        "panssaroitu = panssari, aseesta riippumatta. Luvut ovat "
        "**sisäkkäisiä**: aseistetut ovat panssaroitujen osajoukko, molemmat "
        "on luettu samalta tickiltä samasta pelaajajoukosta, ja jakaja on "
        "sama. Rivien ero on siis se havainto -- pistoolikierroksella "
        "aseistettuja on tyypillisesti 0 (800 $ ei riitä sekä kevlariin että "
        "parannettuun aseeseen), joten panssaririvi on se, joka kertoo "
        "kevlarien määrän."
    )
    holding = (
        "Molemmat luvut ovat **hallussapitoa eivätkä ostoja**: panssari ja ase "
        "säilyvät kierroksen yli hengissä selvinneellä, eikä vaurioitunutta "
        "panssaria eroteta ehjästä. Poikkeus on pistoolikierros -- puoliaika "
        "alkaa puhtaalta pöydältä, joten siellä luvut kertovat mitä ostettiin."
    )
    if flags.armed_shown and flags.armored_shown:
        return [nesting, holding]
    if flags.armored_shown:
        return [
            "Panssaroitu = panssari ostoajan lopussa, aseesta riippumatta; "
            "kypärää ei eroteta. Luku on **hallussapitoa eikä ostos**: "
            "panssari säilyy kierroksen yli hengissä selvinneellä. Poikkeus on "
            "pistoolikierros, jolla puoliaika alkaa puhtaalta pöydältä -- "
            "siellä luku kertoo montako kevlaria ostettiin."
        ]
    if flags.armed_shown:
        return [
            "Aseistettu = panssari JA parannettu ase ostoajan lopussa. Se ei "
            "ole sama asia kuin kevlarien määrä: pistoolikierroksella luku on "
            "tyypillisesti 0, vaikka kaikilla olisi panssari, koska 800 $ ei "
            "riitä sekä kevlariin että parannettuun aseeseen. Luku on "
            "**hallussapitoa eikä ostos**: säästetty tai poimittu ase "
            "lasketaan samoin kuin ostettu."
        ]
    return []


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
