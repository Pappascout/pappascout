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

**Karsinta koskee esitystä eikä sisältöä** (Story 2.13). Viisi
``[report]``-osion asetusta jättävät toistoa kirjoittamatta: kylläisen
kalustorivin, identtisen kalustoriviparin toisen puolen, nimetyn
näytepisteen sekä utilityn kohteet ja tappoalueet yleisimpiä lukuun
ottamatta. Kolme sääntöä pätee jokaiseen:

**Rivit rakennetaan ensin, karsitaan vasta sitten.** Kynnyksen pudottamat
havainnot lasketaan rivinrakentajassa, joten oikosulku ennen sitä
pienentäisi lohkon huomautusta -- eli karsinta muuttaisi **väitettä
datasta**. Sama järjestys ratkaisee myös sen, milloin sääntö *ei* poistanut
mitään: rivi, jota kuviosuodatus ei päästänyt syntymään, ei ole karsittu.

**Mikään ei katoa hiljaa.** Jos rivi jätetään kirjoittamatta, lukuohje
kertoo kertaalleen mitä sen puuttuminen tarkoittaa
(:func:`_pruning_legend`); jos riviltä jää pois väitteitä, rivi kertoo
pudotettujen määrän (:func:`_dropped_note`) -- sama sääntö kuin
kuviosuodatuksella. Selitys kirjoitetaan vain säännöstä, joka oikeasti
karsi jotakin, ja se nimeää asetuksensa.

**Osa kierrostyypeistä on suojattu** (:data:`PROTECTED_ROUND_TYPES`), ja
jokainen lukuohjeen karsintakappale sanoo sen ääneen: sama raportti
sisältää karsimattomia lohkoja, joten ehdoton lause olisi väärä.

``Report``, ``report.json`` ja ``REPORT_SCHEMA_VERSION`` eivät muutu, ja
jokainen karsittu arvo on niissä yhä -- se vain jää kertomatta *tässä*
raportissa. Mitatut perusteet ja luvut ovat ``settings.toml``issa ja
READMEssä; mittausdokumentit itse asuvat BMAD-tuotoksissa eivätkä tässä
repossa.

**Kuolemat mahtuvat kahteen riviin.** Raportti on jo satoja rivejä, kun
Veetin oma analyysi on 30. Kuolemat lisättiin siksi, että ne selittävät muut
rivit -- ei siksi, että ne olisivat oma lukunsa. Raja on
:data:`MAX_DEATH_LINES`, ja sen ylitys on virhe eikä hiljainen kasvu.

**Runko puhuu nimillä, ja tunnisteilla on oma lukunsa.** Joukkueen ja
kokoonpanojen tiivisteet, pelaajien SteamID64 ja karttojen demotunnisteet
eivät ole rungossa vaan luvussa :data:`TRACEABILITY_HEADING`. Sääntö on
täällä eikä vain funktioiden sisällä, koska se koskee kuutta niistä
(:func:`_title`, :func:`_team_text`, :func:`_roster_text`, :func:`_summary`,
:func:`_traceability`, :func:`_anomaly_map_label`) -- yhden sisällä
kirjoitettuna se ei kertoisi, että poikkeuksia on tarkalleen kolme:

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from pappascout.constants import (
    ANOMALY_RULE_FI,
    ANOMALY_RULES,
    ROUND_TYPE_FI,
    SAMPLE_BUCKET_FI,
    SAMPLE_BUCKETS,
    UTILITY_BUCKET_ALL,
    UTILITY_BUCKET_UNKNOWN,
    seconds_label,
)
from pappascout.domain.models import PLAYERS_ON_SERVER, ReportSettings
from pappascout.domain.report import (
    Anomaly,
    AnomalyRound,
    AnomalyScan,
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
    "PROTECTED_ROUND_TYPES",
    "MERGED_EQUIPMENT_LABEL",
    "MAX_DEATH_LINES",
    "KILL_SAMPLE_UNIT",
    "UNKNOWN_AREA",
    "TRACEABILITY_HEADING",
    "ANOMALY_HEADING",
    "MAX_ANOMALY_LINES",
    "UNKNOWN_MAP_LABEL",
    "UNNAMED_PLAYER",
    "Claim",
    "Line",
    "SummaryItem",
    "AnomalyView",
    "RoundTypeView",
    "SideView",
    "MapView",
    "ReportView",
    "build_view",
    "round_list_demo_ids",
    "pattern_min_rounds",
    "rounds_text",
    "demos_text",
    "players_text",
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

#: Kierrostyypit, joita **ei karsita yhdelläkään säännöllä** (Story 2.13).
#:
#: Kaksi tyyppiä, kaksi eri perustetta -- ja molemmissa **kalustorivi on se
#: havainto**, jonka karsinta poistaisi:
#:
#: ``pistol``
#:     Story 2.8 mittasi, että panssariluku on ostohavainto vain
#:     pistoolikierroksella: muualla se on hallussapitoa, joka periytyy
#:     edelliseltä kierrokselta hengissä selvinneellä. Veetin analyysi
#:     käsittelee pistoolikierroksia **kierroksen tarkkuudella** ja muita
#:     kierrostyyppejä kuvioina, ja karsinta seuraa samaa jakoa -- sama jako,
#:     jonka :data:`PATTERN_ROUND_TYPES` tekee toisesta päästä.
#: ``anomaly``
#:     ``classify`` varaa tyypin kahdelle tilanteelle: **havainto on
#:     ristiriitainen** (varustearvo laski ostoaikana) tai **voiton jälkeen
#:     ei ostettu käytännössä mitään**. Kummassakin juuri kalusto on se
#:     havainto, joka teki kierroksesta poikkeaman, joten kylläisen rivin
#:     pudottaminen poistaisi lohkon ainoan syyn olla olemassa. Toisin kuin
#:     pistoolilla, peruste ei ole ostohavainto vs. hallussapito vaan se,
#:     että lohko on **koottu tämän rivin perusteella**.
#:
#: **``ot`` ei ole suojattu, ja se on mittaustulos eikä oletus.** Jatkoajan
#: ensimmäinen kierros näyttää pistoolikierrokselta, mutta
#: ``[league].ot_start_money`` on tässä liigassa 12 500 $, joten jatkoajalla
#: ostetaan täysi kalusto ja ``5/5`` on odotus kuten täydellä ostolla --
#: mitattu arkiston raportista, jossa jatkoaikalohkon ``aseistettuja 5 (3/3)``
#: ja ``panssaroituja 5 (3/3)`` karsiutuivat oikein. **Riippuvuus on
#: kirjoitettava näkyviin, koska se ei ole ilmeinen:** jos ``ot_start_money``
#: joskus laskee pistoolitasolle, ``ot`` on lisättävä tähän luetteloon.
#:
#: Luettelo on tässä eikä asetuksissa, koska se ei ole säädin vaan sen
#: säännön rajaus, jota asetukset säätävät. Sen muuttaminen on
#: sopimusmuutos, ja lukuohje nimeää tyypit ääneen
#: (:func:`_protected_round_types_text`), joten kuudes sääntö perii
#: poikkeuksen selittämisen automaattisesti.
PROTECTED_ROUND_TYPES: frozenset[str] = frozenset({"pistol", "anomaly"})

#: Yhdistetyn kalustorivin otsikko (Story 2.13, sääntö 2).
#:
#: Nimiö kertoo **molemmat** laskurit, koska rivi kantaa molempien luvun:
#: identtinen jakauma tarkoittaa, että sama pylväs on sekä aseistettujen että
#: panssaroitujen havainto. Yhden nimiön käyttäminen ("kalustoa") hukkaisi
#: sen, kumpaa lukua rivi koskee, ja lukuohjeen määritelmät ovat nimenomaan
#: näiden kahden sanan määritelmiä.
MERGED_EQUIPMENT_LABEL = "aseistettuja ja panssaroituja ostoajan lopussa"

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

#: Poikkeamaluvun otsikko **sellaisena kuin malli sen latoo**.
#:
#: Sama peruste kuin :data:`TRACEABILITY_HEADING`illa: otsikkorivin ``## ``
#: omistaa malli, mutta nimi esiintyy myös koodissa ja testeissä, ja testi
#: vartioi että ne ovat samat.
ANOMALY_HEADING = "Poikkeamat"

#: Enintään näin monta poikkeamariviä luvussa.
#:
#: Sama peruste kuin :data:`MAX_DEATH_LINES`illa mutta eri mekanismi: kuolemien
#: rivimäärä on rakenteellinen (kaksi reunajakaumaa), joten sen ylitys on
#: virhe. Poikkeamien määrä on **aineiston** ominaisuus, joten virhe kaataisi
#: ajon aineistosta jota ei voi valita -- ja luku on raportin ensimmäinen
#: sisältöluku, joten se ei myöskään saa kasvaa rajatta.
#:
#: Ratkaisu on kuvion kynnyksen kanssa sama: rajaus tehdään ja **pois
#: jätettyjen määrä kirjoitetaan näkyviin**. 20 riviä on jo koko luku; sen yli
#: menevä määrä tarkoittaa, että sääntö laukeaa liian usein, ja siihen vastaa
#: speksin Ask First -portti (yli 20 % osumatiheys) eikä raportin muotoilu.
MAX_ANOMALY_LINES = 20

#: Tunnistamattoman kartan nimiö. Muotoiltava, koska järjestysluku erottaa
#: kaksi tunnistamatonta karttaa toisistaan.
#:
#: **Yksi kirjoitusasu kahdelle luvulle.** Sekä jäljitettävyysluvun
#: karttarivi (:func:`_map_label`) että poikkeamarivi
#: (:func:`_anomaly_map_label`) käyttävät tätä, ja lukija yhdistää rivit
#: nimenomaan merkkijonon perusteella. Kahtena kirjoitettuna toisen
#: muuttaminen katkaisisi yhteyden hiljaa.
UNKNOWN_MAP_LABEL = "kartta {index}, nimeä ei tunnistettu"


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
class AnomalyView:
    """Yksi poikkeama: koontirivi ja sen kierrosrivit.

    Kaksitasoinen tarkoituksella. Koontirivi kantaa otannan ja orientaation,
    kierrosrivit sen mitä kullakin kierroksella havaittiin -- ja juuri
    kierrosrivi on se, joka estää rivin lukemisen väärin: crunchin
    lähtösuunnat ovat yhtäaikaisia vain saman kierroksen sisällä, joten
    kahden kierroksen yhdiste väittäisi useampaa samanaikaista suuntaa kuin
    havaittiin.

    ``rounds`` on valmiiksi muotoiltuja merkkijonoja eikä
    :class:`Line`-olioita: niillä ei ole omaa otantaa, joten :class:`Claim`in
    sopimus ("väitettä ei voi rakentaa ilman otantaa") ei päde niihin. Rivien
    otanta on koontirivillä, jonka alle ne kuuluvat.
    """

    rule: str
    line: Line
    rounds: tuple[str, ...] = ()


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
    #: Poikkeamaluvun rivit -- kaikki kartat ja puolet samassa jonossa,
    #: kuten ``Report.anomalies``issa. Tyhjä jono on kelvollinen tila.
    anomalies: tuple[AnomalyView, ...] = ()
    #: Teksti, joka luetaan poikkeamarivien **sijasta**, kun niitä ei ole.
    #: :func:`build_view` täyttää aina täsmälleen toisen näistä kahdesta:
    #: poikkeamaluku on olemassa myös silloin, kun poikkeamia ei ole, ja
    #: silloin se sanoo ääneen mitä tutkittiin ja mikä jäi sokeaan pisteeseen
    #: (:func:`_no_anomalies_text`).
    anomalies_note: str | None = None
    #: Huomautus rivikaton pudottamista poikkeamista. Erillinen
    #: :attr:`anomalies_note`sta, koska nämä kaksi ovat eri tiloja: toinen on
    #: "ei löytynyt", toinen "löytyi enemmän kuin luku näyttää".
    anomalies_dropped_note: str | None = None


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

    **Kaksi eri lajia lippuja, ja niiden ero on karsinnan takia olennainen**
    (Story 2.13). ``dropped`` on **kuviosuodatuksen kirjanpitoa**: se kertoo,
    montako havaintoa kynnys pudotti, eikä karsinta saa muuttaa sitä --
    lohkon huomautus on väite datasta. Kaikki muut ovat **esitystä**: ne
    selittävät rivejä, jotka raportissa ovat. Siksi rivinrakentajille
    annetaan oma :class:`_Flags` ja se yhdistetään :meth:`absorb`illa, joka
    siirtää kirjanpidon aina ja esityksen vain jos rivi jäi raporttiin.
    """

    unknown_area: bool = False
    estimated_area: bool = False
    armed_shown: bool = False
    armored_shown: bool = False
    kills_shown: bool = False
    dropped: int = 0

    # -- Karsinta (Story 2.13). Yksi lippu per sääntö, koska lukuohje selittää
    # **vain ne säännöt, jotka oikeasti karsivat jotakin**: sääntö, joka ei
    # osunut kertaakaan, selittäisi puuttuvaa riviä jota ei ole, ja se olisi
    # väite raportista joka ei pidä. Liput nostetaan siksi vasta kun rivi on
    # rakennettu ja tiedetään, että se olisi kirjoitettu.
    #: Sääntö 1 pudotti vähintään yhden kylläisen kalustorivin.
    saturated_dropped: bool = False
    #: Sääntö 2 kirjoitti vähintään yhden kalustorivin yhdistettynä.
    equipment_merged: bool = False
    #: Sääntö 3 jätti nämä näytepisteet kirjoittamatta -- nimiöt sellaisina
    #: kuin ne rivillä olisivat lukeneet (``"45"``), jotta lukuohje voi nimetä
    #: puuttuvan rivin samalla luvulla kuin muut rivit näyttävät omansa.
    skipped_samples: list[str] = field(default_factory=list)
    #: Sääntö 4 lyhensi vähintään yhtä utilityn kohderiviä.
    #:
    #: Totuusarvo eikä laskuri: pudotettujen määrä on **rivin perässä**, jossa
    #: se koskee sitä riviä, ja koko raportin summa ei kertoisi lukijalle
    #: mitään lisää. Lippu vastaa vain kysymykseen "selitetäänkö sääntö".
    utility_targets_capped: bool = False
    #: Sääntö 5 lyhensi vähintään yhtä tapporiviä. Sama peruste.
    kill_areas_capped: bool = False

    def absorb(self, other: "_Flags", *, keep: bool) -> None:
        """Yhdistä yhden rivin liput koko raportin kirjanpitoon.

        ``dropped`` siirtyy **aina**: se on kuviosuodatuksen kirjanpitoa, ja
        sen on oltava sama luku riippumatta siitä, karsittiinko rivi. Ilman
        tätä lohkon huomautus ("N harvinaisempaa havaintoa jäi pois")
        pienenisi karsinnan mukana, eli karsinta muuttaisi **väitettä
        datasta** -- juuri sen, mitä "karsinta koskee esitystä eikä sisältöä"
        kieltää.

        Esitystä koskevat liput siirtyvät vain kun ``keep`` on tosi eli kun
        rivi jäi raporttiin. Muuten lukuohje selittäisi tuntemattoman alueen
        tai arvion riviltä, jota lukija ei näe.
        """
        self.dropped += other.dropped
        if not keep:
            return
        self.unknown_area |= other.unknown_area
        self.estimated_area |= other.estimated_area
        self.armed_shown |= other.armed_shown
        self.armored_shown |= other.armored_shown
        self.kills_shown |= other.kills_shown
        self.saturated_dropped |= other.saturated_dropped
        self.equipment_merged |= other.equipment_merged
        self.utility_targets_capped |= other.utility_targets_capped
        self.kill_areas_capped |= other.kill_areas_capped
        for label in other.skipped_samples:
            if label not in self.skipped_samples:
                self.skipped_samples.append(label)


@dataclass(frozen=True)
class _UseEntry:
    """Yksi utilityn kohderivin väite ja se, mitä siitä pitää selittää.

    Nimetyt kentät eivätkä tuple, koska ``estimated`` ja ``unknown`` ovat
    **eri asioita**: edellinen on johdettu räjähdysalue ("(arvio)") ja
    jälkimmäinen alue, jonka nimeä ei saatu. Yhteen lippuun niputettuina
    toinen selitys ilmestyisi lukuohjeeseen toisen takia -- ja lukuohje
    selittää vain sen, mikä rivillä näkyy.

    ``target`` on räjähdysalue **raakana** (``None`` = ei nimeä), koska
    sääntö 4 rajaa kohteita: sama alue eri heittoalueelta tai eri
    aikaikkunasta on sama kohde, ja nimiön muotoilu (arviomerkintä, ikkuna)
    ei kuulu vertailuun.
    """

    #: Järjestysavain: yleisin ensin, tasatilanteessa teksti.
    rank: tuple[int, str]
    claim: Claim
    target: str | None
    estimated: bool
    unknown: bool


@dataclass(frozen=True)
class _Row:
    """Yksi rakennettu rivi ja se, mitä karsinta siitä päätti.

    Rivit rakennetaan **kertaalleen ja karsitaan vasta sitten**, ja tämä olio
    on se, mikä tekee järjestyksestä mahdollisen. Kaksi syytä:

    1. **Kynnyksen kirjanpito.** Rivinrakentaja on ainoa paikka, joka laskee
       kynnyksen pudottamat havainnot. Jos karsinta ohittaisi rakentajan,
       lohkon huomautus pienenisi karsinnan mukana ja väittäisi datasta
       jotakin muuta kuin karsimaton raportti.
    2. **Lohko, joka tyhjenisi.** Paluu karsimattomaan ei vaadi toista
       rakennuskierrosta, koska karsimaton rivi on tallessa
       (:attr:`plain`) -- eikä siis myöskään sitä, että rivinrakentajat
       ajettaisiin kahdesti samoilla luvuilla.
    """

    #: Rivi ilman karsintaa.
    plain: Line
    #: Rivi karsinnan jälkeen. ``None`` = karsinta pudotti sen kokonaan.
    kept: Line | None
    #: Rivinrakentajan liput. ``None`` = liput on jo yhdistetty suoraan
    #: kirjanpitoon, koska karsinta ei voi pudottaa tätä riviä.
    flags: _Flags | None = None
    #: Yhdistetäänkö esitysliput, vaikka rivi itse ei jäisi. Yhdistetty
    #: kalustorivi (sääntö 2) kantaa **molempien** laskurien luvun, joten
    #: molempien määritelmät tarvitaan lukuohjeeseen, vaikka toinen rivi ei
    #: ole raportissa omanaan.
    keep_flags: bool = False


@dataclass(frozen=True)
class _Pruning:
    """Yhden kierrostyypin karsintasäännöt valmiiksi ratkaistuina.

    Olio eikä asetusosio suoraan, koska **suojatulla kierrostyypillä jokainen
    sääntö on pois** (:data:`PROTECTED_ROUND_TYPES`): ilman yhtä paikkaa,
    jossa poikkeus ratkaistaan, sama ``if`` toistuisi viidessä funktiossa ja
    kuudes lisäys unohtaisi sen. Rakentaja on siis ainoa paikka, joka tietää
    kierrostyypin, ja loput koodi lukee valmiita arvoja.

    :meth:`off` on suojatun kierrostyypin sääntö. Lohkon tyhjenemiseen sitä
    **ei tarvita**: karsimattomat rivit ovat tallessa :class:`_Row`issa, joten
    paluu ei vaadi toista rakennuskierrosta -- eikä siten myöskään sitä, että
    rivin katkaisu (säännöt 4 ja 5) peruttaisiin. Katkaisu ei voi tyhjentää
    lohkoa, joten sen peruminen palauttaisi vain sen 5-9 alkion luettelon,
    jota vastaan koko tarina on kirjoitettu.
    """

    #: Sääntö 1.
    drop_saturated: bool
    #: Sääntö 2.
    merge_equal: bool
    #: Sääntö 3: näytepisteiden nimiöt (``{"45"}``) eikä liukuluvut. Täsmäys
    #: tehdään siinä muodossa, jossa luku on rivillä, joten ``45`` ja ``45.0``
    #: tarkoittavat samaa riviä eikä liukulukuvertailu voi mennä ohi.
    skipped_seconds: frozenset[str]
    #: Sääntö 4; ``0`` = ei rajaa.
    max_utility_targets: int
    #: Sääntö 5; ``0`` = ei rajaa.
    max_kill_areas: int

    @classmethod
    def for_round_type(
        cls, settings: ReportSettings, round_type: str
    ) -> "_Pruning":
        if round_type in PROTECTED_ROUND_TYPES:
            return cls.off()
        return cls(
            drop_saturated=settings.drop_saturated_equipment_lines,
            merge_equal=settings.merge_equal_equipment_lines,
            skipped_seconds=frozenset(
                seconds_label(value) for value in settings.skip_sample_seconds
            ),
            max_utility_targets=settings.max_utility_targets,
            max_kill_areas=settings.max_kill_areas,
        )

    @classmethod
    def off(cls) -> "_Pruning":
        """Karsinta pois: raportti on se, joka oli ennen Story 2.13:a."""
        return cls(
            drop_saturated=False,
            merge_equal=False,
            skipped_seconds=frozenset(),
            max_utility_targets=0,
            max_kill_areas=0,
        )

    def skips(self, position: Position) -> bool:
        """Jätetäänkö tämä näytepiste kirjoittamatta (sääntö 3)."""
        return _sample_key(position) in self.skipped_seconds


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


def players_text(count: int) -> str:
    """``1 pelaaja`` / ``5 pelaajaa``.

    Yksikkö on tässä havainto eikä kielioppikoriste: kalibroinnin kuudesta
    merkitystä kierroksesta neljä osuu **yhden** pelaajan havainnolla, joten
    juuri se muoto toistuu raportissa useimmin.
    """
    return "1 pelaaja" if count == 1 else f"{count} pelaajaa"


def _seconds(value: float) -> str:
    """Sekuntiluku suomalaisella desimaalipilkulla: ``9.0 -> '9'``.

    Muotoilu itse on :func:`~pappascout.constants.seconds_label`issa, koska
    **asetus ja rivi joutuvat olemaan siitä samaa mieltä** (Story 2.13):
    ``[report].skip_sample_seconds`` nimeää näytepisteen sillä luvulla, jonka
    lukija näkee rivillä, ja latausvaiheen tarkistus "kaksi arvoa näyttäisi
    rivillä samalta" käyttää samaa funktiota. Kahtena kopiona ne sopisivat
    vain tänään.

    Nimi jää tähän, koska tämä moduuli käyttää sitä kahdessakymmenessä
    paikassa eikä yksikään niistä ole asetusten täsmäystä.
    """
    return seconds_label(value)


def _median_seconds(value: float) -> str:
    """Mediaaniaika yhdellä desimaalilla.

    Ensikontaktin mediaani on liukuluku tickeistä (``12.531``), ja sen
    kolmas desimaali on tarkkuutta, jota ei ole olemassa: näyte on
    kierroksen ensimmäinen osuma, ei mittaustulos. Yksi desimaali kertoo
    saman ilman että se näyttää tarkemmalta kuin on.
    """
    return f"{value:.1f}".replace(".", ",")


def _area(name: str | None) -> str:
    """Aluenimi turvallisena Markdownina, tai merkintä puuttuvasta.

    **Alue on demon antamaa tekstiä** (``m_szLastPlaceName``) siinä missä
    joukkueen nimi ja kartan nimi: peli antaa arvon, eikä sitä validoida
    mitään luetteloa vasten. Kaikilla oikeilla CS2-alueilla escapetus on
    näkymätön (``BombsiteA``, ``TSideUpper``), mutta workshop-kartan alue
    ``*|Aim|* Botz [beta]`` katkaisisi rivin -- ja se rivi kantaa väitteen
    otannan.

    **Escapetus eikä koodijakso**, ja jako on projektin oma: nimi paetaan
    (joukkue, rosteri), tunniste käärivät koodijaksoon (demotunniste, kartan
    nimi jäljitettävyysluvussa ja karttaluvun otsikossa, kierrosliitteen
    polku), koska tunniste on voitava kopioida raportista sellaisenaan.
    Aluenimeä ei kopioida mihinkään: se luetaan lauseen osana keskellä
    riviä, ja koodijakso katkoisi jokaisen havaintorivin kolmeen palaan.

    ``None`` on **havainnon puuttuminen eikä nimi**, joten se saa oman
    merkintänsä eikä kulje escapetuksen läpi.
    """
    return markdown_text(name) if name else UNKNOWN_AREA


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


def _threshold_value(report: Report, name: str) -> int | float | None:
    """Yksi kynnysarvo ``report.json``ista, tyypittämättä.

    **Yksi haku kahdelle lukijalle.** Arvo luetaan **raportista eikä
    asetuksista**, ja jos sitä ei ole, rivi kirjoitetaan ilman kynnystä sen
    sijaan että renderöinti keksisi oman luvun. Sääntö oli kirjoitettu kahteen
    kertaan (:func:`_threshold_int` ja :func:`_threshold_float`) juuri sen
    kanssa perusteltuna, ettei sitä saa kirjoittaa kahdesti -- ja kopiot
    olivat jo erkaantuneet: toinen vaati positiivista arvoa, toinen hyväksyi
    nollan ja negatiiviset.

    Tämä funktio kantaa **jaetut** ehdot: osio on olemassa, avain on
    olemassa, arvo on luku. Kutsujille jää **kaksi** ehtoa, ja molemmat ovat
    tyypin sanelemia: sallittu tyyppi (``int`` vs. mikä tahansa luku) ja
    alaraja siinä muodossa, jonka tyyppi vaatii (``>= 1`` lukumäärälle,
    ``> 0`` osuudelle). Alarajat eivät ole sama ehto kahdesti vaan sama
    sääntö -- kynnys on positiivinen -- kahdessa yksikössä; ilman eroa
    ``advance_t_share = 0,80`` putoaisi pois.

    ``bool`` hylätään erikseen, koska Pythonissa se on ``int``: ``True``
    latoisi kynnykseksi luvun 1.
    """
    section = report.thresholds_used.get("thresholds")
    if not isinstance(section, Mapping):
        return None
    value = section.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _threshold_int(report: Report, name: str) -> int | None:
    """Kokonaislukukynnys: **kokonaisluku ja vähintään yksi**.

    Alaraja on lukumäärän määritelmä eikä lisäehto: nämä kynnykset ovat
    kierroksia, pelaajia, havaintoja ja alueita, ja "vähintään nolla
    kierrosta" ei ole kynnys vaan sen puuttuminen.
    """
    value = _threshold_value(report, name)
    if not isinstance(value, int) or value < 1:
        return None
    return value


def _threshold_float(report: Report, name: str) -> float | None:
    """Liukulukukynnys: **äärellinen ja positiivinen**.

    Alaraja on eri muodossa kuin :func:`_threshold_int`illä ja samasta
    syystä: liukulukukynnykset ovat osuuksia ja sekunteja, joista pienin
    käytössä oleva on ``advance_t_share = 0,80``. Kokonaisluvun alaraja
    hylkäisi sen. Molemmat hylkäävät silti nollan ja negatiivisen, koska
    ``ThresholdSettings`` vaatii jokaiselta näistä positiivista arvoa --
    nollan päästäminen läpi latoisi raporttiin kynnyksen, jota asetukset
    eivät salli.

    Äärettömyys ja NaN hylätään, koska ne latoutuisivat riville muodossa
    ``inf`` ja ``nan``: luku, jota lukija ei voi tulkita, on huonompi kuin
    puuttuva luku.
    """
    value = _threshold_value(report, name)
    if value is None:
        return None
    number = float(value)
    if not isfinite(number) or number <= 0.0:
        return None
    return number


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
        # Sama sääntö kuin ensimmäisen kuoleman rivillä
        # (:func:`_first_death_label`): mediaani on väite, ja karsinta voi
        # viedä kaikki aluevaateet ja jättää sen yksin. Otanta kirjoitetaan
        # vain silloin, kun rivillä ei ole muuta otantaa -- muuten sama luku
        # olisi rivillä kahdesti ja jokainen toimiva rivi muuttuisi.
        label=_position_label(position, with_sample=not claims),
        claims=tuple(item[3] for item in claims),
        note=note,
    )


def _sample_key(position: Position) -> str | None:
    """Näytepisteen tunnus karsinnalle: sekuntiluku **rivin nimiön muodossa**.

    Täsmäys tehdään merkkijonona eikä liukulukuvertailuna, jotta asetus
    ``45`` ja näytepiste ``45.0`` tarkoittavat samaa riviä: asetuksen arvo on
    ihmisen kirjoittama TOML-luku ja näytepiste raportin liukuluku, eikä
    niiden tarvitse olla tavu tavulta sama arvo täsmätäkseen siihen, mitä
    lukija rivillä näkee.

    ``None`` ensikontaktille: se ei ole valittu näytepiste vaan kierroksen
    oma hetki, eikä sillä ole nimellistä sekuntilukua, jolla sen voisi
    asetuksessa nimetä.
    """
    if position.sample_kind != "time" or position.seconds is None:
        return None
    return _seconds(position.seconds)


def _keep_most_common(
    entries: Sequence[Any], limit: int, n_of: Callable[[Any], int]
) -> tuple[list[Any], int]:
    """Yleisimmät ``limit`` kappaletta, **tasatilanne mukaan luettuna**.

    ``limit <= 0`` tarkoittaa "ei rajaa": rivin tyhjentäminen ei ole
    karsintaa vaan vaimennus, joka on rajattu Story 2.13:sta ulos, joten
    nollaa ei tulkita rajaksi.

    **Tasatilanne jatkaa rajaa.** Jos katkaisukohdan jälkeen on yhtä yleinen
    havainto kuin viimeinen säilytetty, se säilytetään myös: muuten rivi
    pudottaisi kahdesta identtisen otannan havainnosta toisen ja huomautus
    kutsuisi sitä *harvinaisemmaksi*, mikä on väärä väite. Kolmen rajalla
    neljä yhtä yleistä aluetta tuottaa siis neljä aluetta -- raja on
    "yleisimmät", ei "enintään kolme".

    ``entries`` on **oletettava valmiiksi järjestetyksi** yleisimmästä
    harvinaisimpaan; kutsuja tekee sen jo, koska rivin järjestys on sen oma
    päätös.

    Returns:
        ``(säilytetyt, pudotettujen määrä)``.
    """
    if limit <= 0 or len(entries) <= limit:
        return list(entries), 0
    cutoff = n_of(entries[limit - 1])
    kept = [
        entry
        for index, entry in enumerate(entries)
        if index < limit or n_of(entry) == cutoff
    ]
    return kept, len(entries) - len(kept)


def _position_label(position: Position, *, with_sample: bool = False) -> str:
    """Näytepisteen otsikko: ``15 s`` tai ``ensikontakti (mediaani 9 s)``.

    ``with_sample`` lisää **mediaanin oman otannan**, ja se on kutsujan päätös
    eikä tämän funktion: otanta kirjoitetaan vain, kun rivillä ei ole yhtään
    aluevaadetta. Ensikontaktin mediaani on ajoitusväite siinä missä
    ensimmäisen kuoleman mediaani, ja karsinta voi viedä sen alta kaikki
    aluerivit -- silloin rivistä jäisi
    ``ensikontakti (mediaani 14,2 s): näyte puuttuu 2 kierrokselta``, eli
    väite ilman otantaa.

    **Aikanäytepiste ei saa otantaa**, vaikka sekin voi jäädä pelkäksi
    huomautukseksi. Sen nimiö (``15 s``) ei ole väite vaan hetken nimi:
    ilman aluevaateita rivillä ei ole mitään, mistä otanta kertoisi.

    Otanta on ``m / (m + rounds_missing)``: kierrokset, joilla **tämä
    näytepiste on olemassa**, kaikista kierrostyypin kierroksista. Juuri
    niiltä mediaani on laskettu.
    """
    if position.sample_kind == "time":
        # Malli takaa, ettei aikanäytepisteellä voi olla tyhjää sekuntilukua.
        return f"{_seconds(position.seconds or 0.0)} s"
    if position.seconds_median is None:
        return "ensikontakti"
    median = _median_seconds(position.seconds_median)
    if not with_sample:
        return f"ensikontakti (mediaani {median} s)"
    rounds = position.m + position.rounds_missing
    return f"ensikontakti (mediaani {median} s, {position.m}/{rounds} kierroksesta)"


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
    uses: Sequence[UtilityUse], min_n: int, flags: _Flags, pruning: _Pruning
) -> list[Line]:
    """Rivit "mistä minne" -- tavoiteanalyysin "T-spawnista CT-savu B sitelle".

    Yksi rivi **kranaattityyppiä kohden**, ei heittoa kohden. Kuvion kaikki
    heitot ovat samalla rivillä, koska raportti luetaan ottelua edeltävässä
    kiireessä: viisi savuriviä peräkkäin vie viisi riviä kertoakseen yhden
    asian ("savut menevät B:lle").

    **Sääntö 4 (Story 2.13) rajaa kohteita eikä väitteitä.** Mitattuna
    kymmenellä rivillä oli kohteita viidestä yhdeksään, ja sellainen rivi on
    luettelo eikä kuvio. Kohde on **räjähdysalue**, ja sama kohde voi olla
    rivillä useammin kuin kerran: eri heittoalueelta tai eri aikaikkunassa
    (``[aggregate].utility_seconds_buckets``). Väitteitä rajaamalla kaksi
    säilytettyä paikkaa voisi olla sama kohde kahdessa ikkunassa, jolloin
    rivi menettäisi jokaisen eri kohteen samalla kun huomautus kutsuu niitä
    kohteiksi -- ja juuri se on se virhe, jota mitattu peruste ei tarkoita.

    Säilytetyn kohteen **jokainen** väite jää riville: rivi kertoo mihin
    utility menee, ja sama kohde kahdessa ikkunassa on kaksi eri havaintoa
    samasta kohteesta.

    Järjestys on väitteiden oma (yleisin ensin), ei kohteittain ryhmitelty:
    ilman rajaa rivi on **merkki merkiltä** se, joka oli ennen Story 2.13:a.
    """
    rows: dict[str, list[_UseEntry]] = {}
    for use in uses:
        if use.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        target = _area(use.detonate_area)
        estimated = use.area_source == "point_cloud"
        text = (
            f"{_area(use.throw_area)} -> {target}"
            f"{ESTIMATE_MARK if estimated else ''}"
            f"{_bucket_text(use.seconds_bucket)}"
        )
        extra = f"{use.throws} heittoa" if use.throws != use.n else None
        unknown = use.throw_area is None or use.detonate_area is None
        rows.setdefault(use.grenade_type, []).append(
            _UseEntry(
                rank=(-use.n, text),
                claim=Claim(text=text, n=use.n, m=use.m, extra=extra),
                target=use.detonate_area,
                estimated=estimated,
                unknown=unknown,
            )
        )

    lines: list[Line] = []
    for grenade_type in sorted(rows, key=_grenade_rank):
        entries = sorted(rows[grenade_type], key=lambda entry: entry.rank)
        kept, dropped = _kept_targets(entries, pruning.max_utility_targets)
        if dropped:
            flags.utility_targets_capped = True
        # Liput vasta säilytetyistä väitteistä ja **erikseen**: pudotetun
        # väitteen arvio tai tuntematon alue selittäisi lukuohjeessa rivin,
        # jota ei ole -- ja arvio (johdettu räjähdysalue) on eri asia kuin
        # tuntematon alue (nimeä ei saatu), joten yhteen lippuun niputettuna
        # toinen selitys ilmestyisi toisen takia.
        for entry in kept:
            flags.unknown_area |= entry.unknown
            flags.estimated_area |= entry.estimated
        lines.append(
            Line(
                label=_grenade(grenade_type),
                claims=tuple(entry.claim for entry in kept),
                note=_dropped_note(dropped, "kohdetta"),
            )
        )
    return lines


def _kept_targets(
    entries: Sequence[_UseEntry], limit: int
) -> tuple[list[_UseEntry], int]:
    """Säilytettävät väitteet, kun raja koskee **kohteita** (sääntö 4).

    Kohteet asetetaan järjestykseen sillä väitteellä, joka niistä on yleisin:
    väitteet ovat jo järjestyksessä, joten kohteen ensiesiintymä kertoo sen
    sijan. Rajaus tehdään kohdejoukolle (tasatilanne mukaan luettuna), ja
    väitteet suodatetaan sen mukaan **alkuperäisessä järjestyksessä**.

    Returns:
        ``(säilytetyt väitteet, pudotettujen kohteiden määrä)``.
    """
    order: list[str | None] = []
    best: dict[str | None, int] = {}
    for entry in entries:
        if entry.target not in best:
            best[entry.target] = entry.claim.n
            order.append(entry.target)
    kept_targets, dropped = _keep_most_common(order, limit, lambda a: best[a])
    if not dropped:
        return list(entries), 0
    allowed = set(kept_targets)
    return [entry for entry in entries if entry.target in allowed], dropped


def _dropped_note(dropped: int, unit: str) -> str | None:
    """Rivin oma huomautus siitä, montako havaintoa siltä jäi pois.

    Sanamuoto on **kuviosuodatuksen sanamuoto** ("119 harvinaisempaa
    havaintoa jäi pois", :func:`_round_type_view`): lukija näkee saman
    lauseen kahdesta eri syystä pois jääneistä havainnoista, ja se on
    tarkoitus -- kyse on samasta asiasta, rivi kertoo mitä siltä puuttuu.
    Yksikkö vaihtuu, koska kohde ja alue ovat eri asioita eikä "havaintoa"
    kertoisi kummasta on kyse.

    ``None`` kun mitään ei pudotettu: tyhjä huomautus latoisi riville
    väliviivan ilman mitään sen perässä.
    """
    if not dropped:
        return None
    return f"{dropped} harvinaisempaa {unit} jäi pois"


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


def _death_lines(
    deaths: DeathReport, min_n: int, flags: _Flags, pruning: _Pruning
) -> list[Line]:
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

    **Yksi seuraus on syytä sanoa ääneen: mediaani voi kadota kokonaan.** Kun
    jokaisella kierroksella on oma kuolema (``rounds_missing == 0``) mutta
    karsinta vie kaikki aluerivit, ehto ei täyty -- ei väitettä, ei
    huomautusta -- eikä riviä kirjoiteta, vaikka ajoitus olisi mitattu. Se ei
    ole väärä väite vaan puuttuva rivi, joten käytös jää ennalleen: raportti
    ei sano mitään, jota se ei voi perustella. Väärä väite olisi mediaani
    ilman otantaa, ja se korjattiin (:func:`_first_death_label`). Jos rivi
    joskus halutaan takaisin, se on **lisätty rivi** eikä muutettu -- ja
    silloin se on epicin sivumitan asia, ei tämän funktion.

    **Sääntö 5 (Story 2.13) koskee vain tapporiviä.** Tapporivi on koko
    kierrostyypin tapot yhdellä rivillä, ja mitattuna siltä karsiutuu 29
    väitettä kolmeen yleisimpään alueeseen rajattuna. Ensimmäisen kuoleman
    riviä **ei rajata**, ja peruste on mitattu eikä pääteltu: sen jakauma on
    kierroksia ja ``Σ n = m``, joten alueita voi olla enintään yhtä monta kuin
    kierroksia -- ja arkiston molemmissa raporteissa (8 demoa, 7 karttahaaraa,
    52 kierrostyyppilohkoa) rivin leveys on **enintään 3 aluetta**, mediaani
    1. Tapporivin nimittäjä
    on tappoja, joten sama alue voi toistua kymmenissä tapoissa ja rivi
    kasvaa riippumatta kierrosten määrästä; juuri se ero tekee toisesta
    luettelon ja toisesta jakauman.
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
                # Otanta otsikkoon **vain kun rivillä ei ole yhtään väitettä**
                # (A2). Karsinta voi viedä kaikki aluerivit ja jättää
                # mediaanin jäljelle, ja silloin rivi väittäisi ajoituksen
                # ilman otantaa -- eli rikkoisi epicin toista kriteeriä.
                # Kun aluerivejä on, ne kantavat otannan jo itse, eikä
                # otsikkoon lisätä mitään: rivi on silloin merkki merkiltä
                # sama kuin ennen tätä tarinaa.
                label=_first_death_label(deaths, with_sample=not claims),
                claims=tuple(claim for _, _, claim in claims),
                note=note,
            )
        )

    kill_claims: list[tuple[int, str, Claim, bool]] = []
    for entry in deaths.kills:
        if entry.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        name = _area(entry.area)
        kill_claims.append(
            (
                -entry.n,
                name,
                Claim(text=name, n=entry.n, m=entry.m, unit=KILL_SAMPLE_UNIT),
                entry.area is None,
            )
        )
    if kill_claims:
        flags.kills_shown = True
        kill_claims.sort(key=lambda item: item[:2])
        kept, dropped = _keep_most_common(
            kill_claims, pruning.max_kill_areas, lambda item: item[2].n
        )
        if dropped:
            flags.kill_areas_capped = True
        # Lippu vasta säilytetyistä alueista: pudotettu tuntematon alue
        # selittäisi lukuohjeessa rivin, jota ei ole.
        if any(unknown for _, _, _, unknown in kept):
            flags.unknown_area = True
        lines.append(
            Line(
                label="tapot alueittain",
                claims=tuple(claim for _, _, claim, _ in kept),
                note=_dropped_note(dropped, "aluetta"),
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


def _first_death_label(deaths: DeathReport, *, with_sample: bool = False) -> str:
    """Otsikko: ``ensimmäinen kuolema (mediaani 24 s)``.

    Mediaani on otsikossa eikä omana väitteenään samasta syystä kuin
    ensikontaktin näytepisteessä: se on koko rivin ajoitus eikä yhden alueen
    havainto.

    **Mutta se on väite, ja väite kantaa otantansa.** Aiempi sanamuoto sanoi,
    ettei mediaanilla ole omaa ``n/m``-otantaa; retro mittasi vastaesimerkin
    (RCAVE ``de_anubis`` default): kun jokainen seitsemästä kuolemasta oli eri
    alueella, karsinta pudotti kaikki aluerivit ja rivistä jäi
    ``ensimmäinen kuolema (mediaani 14,2 s): ei omia kuolemia 2 kierroksella``
    -- ajoitus ilman yhtäkään lukua, joka kertoisi mistä se on laskettu.

    ``with_sample`` on siksi **kutsujan päätös eikä tämän funktion**: otanta
    kirjoitetaan vain silloin, kun rivillä ei ole muuta otantaa. Muuten sama
    luku olisi rivillä kahdesti, ja jokainen toimiva rivi muuttuisi.

    Otanta on ``m / (m + rounds_missing)``: **kierrokset, joilla joukkue
    menetti pelaajan, kaikista kierrostyypin kierroksista**. Juuri ne
    kierrokset mediaani kattaa.

    **Nimittäjä ei ole sama kuin aluerivien, ja se on tarkoituksellista.**
    Aluerivi lukee ``4/7``: sen nimittäjä on ``deaths.m`` eli kierrokset,
    joilla kuolema tapahtui -- jakauman ``Σ n = m`` pätee vain siinä
    populaatiossa. Mediaani lukee ``7/9``: sen nimittäjä on kierrostyypin
    kaikki kierrokset, koska mediaani on koko lohkon ajoitusväite eikä yhden
    alueen osuus. Lukuohjeen yleissääntö (``m`` = kierrostyypin kaikki
    kierrokset) kuvaa siis mediaania; aluerivit ovat siitä nimetty poikkeus,
    ja lukuohje sanoo sen ääneen. **Luvut eivät koskaan ole samalla rivillä**
    -- mediaani saa otannan vain silloin, kun aluerivejä ei ole -- mutta ne
    ovat samassa luvussa peräkkäin, joten ero on kirjoitettava näkyviin.

    Mediaani lasketaan niiden kierrosten ajoituksista; ajoitukseton kuolema
    kaventaisi sitä, mutta arkistossa niitä ei ole (mitattu 3.9.: 1 219
    kuolemaa, 0 ilman ``t_s``:ää), eikä mallissa ole kenttää sen
    erottamiseen -- sellaisen lisääminen olisi skeemamuutos.
    """
    if deaths.first_death_seconds_median is None:
        # Ilman mediaania rivillä ei ole väitettä, vain kattavuushuomio
        # ("ei omia kuolemia N kierroksella"). Otanta kertoisi silloin
        # otannan väitteelle, jota ei ole.
        return "ensimmäinen kuolema"
    median = _median_seconds(deaths.first_death_seconds_median)
    if not with_sample:
        return f"ensimmäinen kuolema (mediaani {median} s)"
    rounds = deaths.m + deaths.rounds_missing
    return (
        f"ensimmäinen kuolema (mediaani {median} s, "
        f"{deaths.m}/{rounds} kierroksesta)"
    )


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


def _equipment_rows(
    report_type: RoundTypeReport,
    min_n: int,
    flags: _Flags,
    pruning: _Pruning,
) -> tuple[list[_Row], bool, bool]:
    """Kalustorivit: aseistetut ja panssaroidut, karsintasäännöt 1 ja 2.

    Rivit ovat yhdessä funktiossa siksi, että molemmat säännöt koskevat
    **paria** eivätkä yksittäistä riviä: sääntö 1 vertaa jakaumaa täyteen
    joukkueeseen ja sääntö 2 vertaa jakaumia toisiinsa. Erikseen
    kirjoitettuina kumpikaan ei näkisi toista, ja yhdistäminen edellyttää
    tietoa siitä, ettei toista riviä jo pudotettu.

    **Rivit rakennetaan ensin, karsitaan vasta sitten.** Järjestys ei ole
    makuasia vaan koko säännön ehto:

    * Rivinrakentaja on ainoa paikka, joka laskee kuviosuodatuksen
      pudottamat pylväät. Oikosulku ennen sitä pienentäisi lohkon
      huomautusta, eli karsinta muuttaisi **väitettä datasta**.
    * Kylläinen rivi voi jäädä kirjoittamatta myös **kynnyksen takia**
      (täydellä ostolla kaksi kierrosta ei riitä kuvioksi). Silloin sääntö 1
      ei poistanut mitään, eikä se saa sanoa lukuohjeessa poistaneensa.

    Järjestys sääntöjen kesken on säännön numero: kylläinen rivi pudotetaan
    **ensin**, koska jos molemmat ovat kylläisiä, yhdistäminen kirjoittaisi
    rivin, joka on juuri se odotus, jonka sääntö 1 jättää sanomatta.

    Args:
        report_type: Kierrostyypin havainnot raportista.
        min_n: Toistumisen kynnys, sama kuin muilla riveillä.
        flags: Raportin laajuinen kerääjä. Tarvitaan tässä siksi, että rivi,
            jota **ei syntynyt lainkaan**, ei mahdu :class:`_Row`iin -- ja
            juuri sen kynnyskirjanpito on se, jonka katoaminen tekisi lohkon
            huomautuksesta väärän.
        pruning: Tämän kierrostyypin karsintasäännöt.

    Returns:
        ``(rivit, pudottiko sääntö 1, yhdistettiinkö sääntö 2:lla)``. Rivit
        ovat :class:`_Row`-olioita, joten kutsuja näkee sekä karsitun että
        karsimattoman muodon eikä joudu rakentamaan mitään toista kertaa.
    """
    armed = report_type.players_armed
    armored = report_type.players_armored
    armed_flags, armored_flags = _Flags(), _Flags()
    armed_line = _armed_line(armed, min_n, armed_flags)
    armored_line = _armored_line(armored, min_n, armored_flags)
    # Rivi, jota kuviosuodatus ei päästänyt syntymään, ei mahdu ``_Row``iin,
    # mutta sen kirjanpito kuuluu lohkolle: ilman tätä lohkon huomautus
    # pienenisi eikä kertoisi, montako havaintoa jäi kynnyksen alle.
    if armed_line is None:
        flags.absorb(armed_flags, keep=False)
    if armored_line is None:
        flags.absorb(armored_flags, keep=False)

    armed_bars = [(bar.armed, bar.n) for bar in armed.counts]
    armored_bars = [(bar.armored, bar.n) for bar in armored.counts]
    drop_armed = (
        pruning.drop_saturated
        and armed_line is not None
        and _is_saturated(armed_bars, armed.rounds_unknown)
    )
    drop_armored = (
        pruning.drop_saturated
        and armored_line is not None
        and _is_saturated(armored_bars, armored.rounds_unknown)
    )
    saturated_dropped = drop_armed or drop_armored

    merge = (
        pruning.merge_equal
        and armed_line is not None
        and armored_line is not None
        and not drop_armed
        and not drop_armored
        and _same_distribution(armed_bars, armored_bars, armed, armored)
    )
    if merge:
        # Uusi nimiö samoille väitteille: rivi kantaa molempien laskurien
        # luvun, joten se on uudelleennimeäminen eikä uusi laskenta.
        merged = Line(
            label=MERGED_EQUIPMENT_LABEL,
            claims=armed_line.claims,
            note=armed_line.note,
        )
        return (
            [
                _Row(plain=armed_line, kept=merged, flags=armed_flags),
                # Panssaririvi ei jää omanaan, mutta sen liput jäävät:
                # lukuohjeen on määriteltävä molemmat sanat, tai yhdistetty
                # rivi kertoisi luvun ilman määritelmää.
                _Row(
                    plain=armored_line,
                    kept=None,
                    flags=armored_flags,
                    keep_flags=True,
                ),
            ],
            saturated_dropped,
            True,
        )

    rows: list[_Row] = []
    if armed_line is not None:
        rows.append(
            _Row(
                plain=armed_line,
                kept=None if drop_armed else armed_line,
                flags=armed_flags,
            )
        )
    # Panssaririvi heti aseistettujen perässä: niiden ero on itse havainto,
    # eikä sitä näe, jos rivien välissä on muuta.
    if armored_line is not None:
        rows.append(
            _Row(
                plain=armored_line,
                kept=None if drop_armored else armored_line,
                flags=armored_flags,
            )
        )
    return rows, saturated_dropped, False


def _is_saturated(bars: Sequence[tuple[int, int]], rounds_unknown: int) -> bool:
    """Onko kalustorivi **kylläinen** eli odotus eikä havainto (sääntö 1).

    Kolme ehtoa, ja jokainen niistä on tarpeen:

    * **Yksi pylväs.** Kaksi pylvästä tarkoittaa, että lukema vaihteli
      kierrosten välillä, ja vaihtelu on havainto.
    * **Arvo on** :data:`~pappascout.domain.models.PLAYERS_ON_SERVER`. Yksi
      pylväs arvolla 0 on yhtä lailla yksitoikkoinen rivi, mutta se on
      havainto: "kenelläkään ei ollut panssaria" on tavoiteanalyysin *"ei
      kevuja"* eikä odotus.
    * **Havainto saatiin joka kierrokselta.** ``rounds_unknown`` yli nollan
      tarkoittaa, että rivillä on huomautus lukukelvottomista kierroksista --
      ja se huomautus on havainto, joka katoaisi rivin mukana.

    ``Σ n = m`` on mallin takaama, joten yhden pylvään tapauksessa ``n = m``
    seuraa siitä eikä sitä tarvitse tarkistaa erikseen.

    **Kylläisyys ei ole ainoa syy**, jonka takia rivi voi jäädä
    kirjoittamatta: kuviosuodatus pudottaa sen täydellä ostolla, jos otanta
    ei riitä kuvioksi. Siksi tätä kysytään vain riviltä, joka oikeasti
    rakennettiin (:func:`_equipment_rows`).
    """
    return (
        len(bars) == 1
        and bars[0][0] == PLAYERS_ON_SERVER
        and rounds_unknown == 0
    )


def _same_distribution(
    armed_bars: Sequence[tuple[int, int]],
    armored_bars: Sequence[tuple[int, int]],
    armed: ArmedPlayers,
    armored: ArmoredPlayers,
) -> bool:
    """Kertovatko kalustorivit **täsmälleen saman** jakauman (sääntö 2).

    Kolme asiaa on verrattava, ei yksi. Pylväät kertovat lukemat, ``m`` on
    väitteiden nimittäjä ja ``rounds_unknown`` rivin huomautus: jos jokin
    näistä eroaa, rivit eroavat, ja ero on juuri se havainto, jota varten
    rivejä on kaksi (Story 2.8).

    Pylväät verrataan järjestettyinä, koska ``report.json``in listajärjestys
    ei ole sopimus.
    """
    return (
        armed.m == armored.m
        and armed.rounds_unknown == armored.rounds_unknown
        and sorted(armed_bars) == sorted(armored_bars)
    )


#: Huomautus lohkosta, jonka karsinta olisi tyhjentänyt.
#:
#: Speksin "Ask First" -portti: sääntö, joka poistaisi lohkosta jokaisen
#: rivin, lakkauttaisi lohkon kertomasta mitään, ja se on **vaimennuspäätös
#: eikä karsinta**. Vaimennus on rajattu Story 2.13:sta ulos, joten koodi
#: tekee sen, mitä matriisi sanoo: rivit säilyvät. Huomautus on siksi, että
#: hiljainen paluu karsimattomaan lukisi kuin sääntöä ei olisi ollut
#: käytössä -- ja seuraava lukija ihmettelisi, miksi juuri tässä lohkossa on
#: rivi, jonka sääntö muualla poistaa.
#:
#: **Paluu koskee vain kokonaan pudotettuja rivejä.** Rivin katkaisu
#: (säännöt 4 ja 5) ei voi tyhjentää lohkoa, joten sitä ei peruta: peruminen
#: palauttaisi vain sen 5-9 alkion luettelon, jota vastaan koko tarina on
#: kirjoitettu.
_PRUNING_KEPT_THE_BLOCK = (
    "Karsinta olisi poistanut tästä lohkosta jokaisen rivin, joten sitä ei "
    "karsittu: tyhjä lohko olisi vaimennus eikä karsinta."
)


def _round_type_lines(
    report_type: RoundTypeReport,
    min_n: int,
    flags: _Flags,
    pruning: _Pruning,
) -> tuple[list[Line], bool]:
    """Yhden kierrostyypin rivit järjestyksessä, karsinta mukaan luettuna.

    **Rivit rakennetaan kertaalleen.** Jokainen rivi syntyy täsmälleen samoin
    kuin ilman karsintaa, ja karsinta tehdään sen jälkeen valmiille riveille:
    kokonaan pudotettu rivi jää :class:`_Row`in ``plain``iin, joten
    lohkon tyhjenemisen tarkistus ei vaadi toista rakennuskierrosta. Tämä on
    myös ainoa tapa, jolla lohkon kuviosuodatuksen huomautus voi olla sama
    karsinnan kanssa ja ilman -- laskuri kasvaa rakentajassa.

    Rivit, joita karsinta ei voi pudottaa (kranaattimäärät, kohderivit,
    ensikontaktin läsnäolo, kuolemat), kirjoittavat lippunsa suoraan
    ``flags``iin: niiden kohtalo ei riipu mistään päätöksestä, joten
    ehdollinen kirjanpito olisi turhaa koneistoa.

    Returns:
        ``(rivit, palautettiinko karsimaton lohko)``.
    """
    rows: list[_Row] = []
    skipped_samples: list[str] = []

    for position in report_type.positions:
        scratch = _Flags()
        line = _position_line(position, min_n, scratch)
        if line is None:
            # Rivi ei syntynyt lainkaan, joten karsinnalla ei ole mitään
            # sanottavaa siitä. Kynnyksen kirjanpito siirtyy silti.
            flags.absorb(scratch, keep=False)
            continue
        if pruning.skips(position):
            label = _sample_key(position)
            if label is not None and label not in skipped_samples:
                skipped_samples.append(label)
            rows.append(_Row(plain=line, kept=None, flags=scratch))
        else:
            rows.append(_Row(plain=line, kept=line, flags=scratch))

    def keep_all(lines: Sequence[Line]) -> None:
        rows.extend(_Row(plain=line, kept=line) for line in lines)

    utility = _utility_count_line(report_type.utility_counts, min_n, flags)
    if utility is not None:
        keep_all([utility])
    keep_all(_utility_use_lines(report_type.utility, min_n, flags, pruning))

    equipment, saturated_dropped, merged = _equipment_rows(
        report_type, min_n, flags, pruning
    )
    rows.extend(equipment)

    gap = _first_contact_gap_line(report_type, min_n, flags)
    if gap is not None:
        keep_all([gap])

    keep_all(_death_lines(report_type.deaths, min_n, flags, pruning))

    kept = [row.kept for row in rows if row.kept is not None]
    if rows and not kept and (saturated_dropped or skipped_samples):
        # Karsinta olisi jättänyt lohkon tyhjäksi: rivit säilyvät
        # karsimattomina, eikä yhtäkään sääntöä merkitä käytetyksi -- lukuohje
        # selittäisi muuten poistoa, jota ei tehty.
        for row in rows:
            if row.flags is not None:
                flags.absorb(row.flags, keep=True)
        return [row.plain for row in rows], True

    for row in rows:
        if row.flags is not None:
            flags.absorb(row.flags, keep=row.keep_flags or row.kept is not None)
    if saturated_dropped:
        flags.saturated_dropped = True
    if merged:
        flags.equipment_merged = True
    for label in skipped_samples:
        if label not in flags.skipped_samples:
            flags.skipped_samples.append(label)
    return kept, False


def _round_type_view(
    report_type: RoundTypeReport,
    threshold: int | None,
    flags: _Flags,
    settings: ReportSettings,
) -> RoundTypeView:
    """Kokoa yhden kierrostyypin rivit.

    Args:
        report_type: Kierrostyypin havainnot raportista.
        threshold: Toistumisen kynnys tai ``None``, jos sitä ei ollut.
        flags: Raportin laajuinen kerääjä. Selitykset (tuntematon alue, arvio,
            aseistettu) kirjoitetaan **kerran** raportin loppuun, joten ne on
            koottava kaikkien kierrostyyppien yli eikä yhden sisällä.
        settings: Karsintasäännöt (Story 2.13). Osio annetaan kokonaisena,
            koska suojattu kierrostyyppi ratkaistaan täällä -- ks.
            :meth:`_Pruning.for_round_type`.
    """
    pattern_only = report_type.round_type in PATTERN_ROUND_TYPES
    dropped_before = flags.dropped
    # Säästökierroksilla jokainen havainto kirjoitetaan (min_n = 1); täysillä
    # ostoilla vain toistuvat. Kynnys tulee raportista, ei täältä.
    min_n = threshold if (pattern_only and threshold is not None) else 1

    pruning = _Pruning.for_round_type(settings, report_type.round_type)
    lines, kept_the_block = _round_type_lines(
        report_type, min_n, flags, pruning
    )

    # Kaksi suodatusta koskevaa asiaa -- sääntö ja sen hinta -- ovat samalla
    # rivillä: raportti on lyhyt, ja kaksi kursivoitua alaviitettä jokaisen
    # default-lohkon perässä maksaisi kahdeksan riviä kartalta kertoakseen
    # yhden asian.
    #
    # **Luku on sama karsinnan kanssa ja ilman.** Se on väite datasta ("näin
    # monta havaintoa ei toistunut riittävästi"), ei esitysvalinta, ja
    # karsinta ei kosketa sitä: rivit rakennetaan ennen karsintaa ja
    # kirjanpito siirtyy myös pudotetuista riveistä (:meth:`_Flags.absorb`).
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
    # Viimeisenä, koska tämä on **poikkeus** eikä lohkon sääntö: kynnyksen
    # huomautus kertoo, miten lohko koottiin, ja tämä kertoo mitä sen jälkeen
    # jäi tekemättä.
    if kept_the_block:
        notes.append(_PRUNING_KEPT_THE_BLOCK)

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


def _anomaly_views(
    report: Report,
) -> tuple[tuple[AnomalyView, ...], str | None]:
    """Poikkeamarivit ja rivikaton huomautus.

    Kaksi paluuarvoa, koska kattoa ei voi soveltaa hiljaa: pois jätettyjen
    määrä kirjoitetaan näkyviin samalla säännöllä kuin kuvion kynnyksen
    pudottamat havainnot.

    **Järjestys on toistumisen mukaan, ei kartan.** Luku on raportin
    ensimmäinen sisältöluku ja sen tehtävä on nostaa esiin se, mikä toistuu;
    tasatilanteessa avain on (kartta, puoli, sääntö, alue), joten yhtä usein
    havaitut pysyvät karttajärjestyksessä ja tulos on sama ajosta toiseen.
    """
    ordered = sorted(report.anomalies, key=_anomaly_rank)
    kept = ordered[:MAX_ANOMALY_LINES]
    dropped = len(ordered) - len(kept)
    note = None
    if dropped:
        note = (
            f"{dropped} poikkeamaa jäi pois: luvussa näytetään enintään "
            f"{MAX_ANOMALY_LINES} useimmin toistuvaa. Kaikki ovat "
            "report.jsonissa kentässä anomalies."
        )
    # Karttojen järjestysluvut samasta lähteestä kuin
    # jäljitettävyysluvussa, jotta tunnistamattoman kartan nimiö on sama
    # merkkijono molemmissa ja lukija voi yhdistää rivit.
    index_of = {
        entry.map_name: index
        for index, entry in enumerate(report.maps, start=1)
    }
    return tuple(_anomaly_view(entry, index_of) for entry in kept), note


def _anomaly_rank(anomaly: Anomaly) -> tuple[int, str, str, int, str]:
    """Useimmin toistuva ensin, sitten kartta, puoli, sääntö ja alue."""
    return (
        -anomaly.n,
        anomaly.map_name,
        anomaly.side,
        ANOMALY_RULES.index(anomaly.rule),
        anomaly.area,
    )


def _anomaly_view(
    anomaly: Anomaly, index_of: Mapping[str, int]
) -> AnomalyView:
    """Yksi poikkeama: koontirivi ja kierrosrivit.

    **Yhtäaikaisuus ei ylitä kierrosrajaa.** Koontirivi kertoo alueen,
    otannan ja orientaation; näytepisteet, suunnat ja pelaajamäärä ovat
    kierrosriveillä, koska vain siellä ne ovat samanaikaisia. Kahden
    kierroksen suuntien yhdiste lukisi useammaksi samanaikaiseksi suunnaksi
    kuin havaittiin -- päinvastoin kuin määritelmä.

    Nimiön kartta kulkee :func:`_anomaly_map_label`in läpi, joten
    tunnistamaton kartta ei tuo demotunnistetta runkoon (Story 2.12).
    """
    label = (
        f"{ANOMALY_RULE_FI[anomaly.rule]} "
        f"({_anomaly_map_label(anomaly, index_of)}, {anomaly.side}-puoli"
        f"{_round_type_suffix(anomaly)})"
    )
    return AnomalyView(
        rule=anomaly.rule,
        line=Line(
            label=label,
            claims=(
                Claim(
                    text=_area(anomaly.area),
                    n=anomaly.n,
                    m=anomaly.m,
                    extra=_anomaly_extra(anomaly),
                ),
            ),
            note="pieni otanta" if anomaly.small_sample else None,
        ),
        rounds=tuple(_anomaly_round_text(anomaly, entry) for entry in anomaly.rounds),
    )


def _anomaly_extra(anomaly: Anomaly) -> str:
    """Rivin lisätieto: se todistuskappale, jonka **tämä sääntö** mittasi.

    Orientaatiosäännöillä se on alueen T-osuus ja sen oma otanta. Stackilla
    sitä ei ole -- sääntö ei lue orientaatiota lainkaan -- ja tilalle tulee
    se, mikä stackissa on johdettua: **ryhmä**. Ilman sitä rivi näyttäisi
    väittävän, että neljä pelaajaa oli yhdellä ``env_cs_place``-alueella;
    juuri sitä sääntö ei väitä eikä voisi väittää, ja siksi ryhmä on
    olemassa.
    """
    if anomaly.rule != "stack":
        return _orientation_text(anomaly)
    # Ryhmän nimi yhdyssanana ("B-siten ryhmässä") eikä sanaparina ("siten B
    # ryhmässä": suomen "siten" luetaan silloin adverbina). Johtamistapa jää
    # lukuohjeeseen -- rivi kertoo havainnon, ei menetelmää.
    return f"{anomaly.site}-siten ryhmässä"


def _anomaly_map_label(anomaly: Anomaly, index_of: Mapping[str, int]) -> str:
    """Kartan nimi poikkeamarivillä, tunnistamaton sanottuna ääneen.

    **Runko puhuu nimillä** (Story 2.12), ja sen kolme poikkeusta eivät kata
    tätä lukua. Kun kartan nimen lähde on ``unknown``, ``map_name`` **on**
    demotunniste (ks. :class:`~pappascout.domain.report.MapReport`), joten
    paljaana se tuo tunnisteen runkoon. Karttaluvun otsikko on poikkeus
    siksi, että siellä tunniste on kartan ainoa nimi; täällä se ei ole,
    koska rivi kertoo kartan lisäksi puolen, alueen ja otannan.

    Nimiö on **sama merkkijono** kuin jäljitettävyysluvun karttarivillä
    (:data:`UNKNOWN_MAP_LABEL`, :func:`_map_label`), joten lukija voi
    yhdistää rivin oikeaan karttalukuun. Järjestysluku on välttämätön: kaksi
    tunnistamatonta karttaa eivät saa saada samaa nimiötä.

    **Nimi suojataan koodijaksona** (:func:`_identifier`), koska se on Story
    2.11:n jälkeen demon antamaa vapaata tekstiä eikä karttapoolia vasten
    validoitu arvo: workshop-kartta nimeltä ``*|Aim|* Botz [beta]`` on
    laillinen havainto, ja paljaana se katkaisisi rivin kesken -- juuri sen
    rivin, joka kantaa poikkeaman otannan. Suojaus puuttui täältä ja
    karttaluvun otsikosta.

    **Saman rivin toinen puolisko on alueen nimi**, ja sekin on demon
    antamaa tekstiä; se suojataan :func:`_area`ssa escapetuksella. Kumpikin
    puolisko yksin jättäisi rivin katkeavaksi, ja juuri se pari jäi
    huomaamatta, kun sääntö oli kirjoitettu vain toisen funktion nimellä.

    Mekanismi on **sama kuin kahdessa muussa paikassa, joissa sama nimi
    latotaan** (:func:`_map_label`, karttaluvun otsikko), eikä valittu
    uudestaan: koodijakso säilyttää täsmälleen samat merkit, kun taas
    escapetus antaisi tälle riville toisen kirjoitusasun kuin karttaluvulle
    -- ja tämän rivin koko tehtävä on ohjata lukija oikeaan karttalukuun.

    Tunnistamattoman kartan nimiö on **meidän omaa tekstiämme**, joten sitä
    ei suojata: siinä ei ole demon antamaa merkkiäkään.
    """
    if anomaly.map_name_source == "unknown":
        return UNKNOWN_MAP_LABEL.format(index=index_of.get(anomaly.map_name, 0))
    return _identifier(anomaly.map_name)


def _round_type_suffix(anomaly: Anomaly) -> str:
    """Kierrostyypit nimiöön -- tai maininta siitä, ettei niitä rajattu.

    Eteneminen ryhmitellään kierrostyypin mukaan, joten sillä on täsmälleen
    yksi ja se kuuluu nimiöön havaintona. Crunch ei tunne kierrostyyppiä:
    sen nimittäjä on puolen kaikki kierrokset, joten nimiö kertoo **millä
    tyypeillä se havaittiin** eikä mihin se on rajattu. Ilman eroa lukija
    lukisi crunchin ``eco``-merkinnän rajaukseksi ja ihmettelisi, miksi
    ``default``-riviä ei ole.
    """
    names = ", ".join(
        ROUND_TYPE_FI.get(name, name) for name in anomaly.round_types
    )
    if anomaly.rule == "ct_advance":
        return f", {names}"
    return f", havaittu: {names}"


def _anomaly_round_text(anomaly: Anomaly, entry: AnomalyRound) -> str:
    """Yhden kierroksen havainto: milloin, kuinka monta ja mistä.

    Kierrosnumero ensin, koska scoutin seuraava teko on avata se kierros
    demolta. Kierrostyyppi on mukana vain crunchilla ja stackilla:
    etenemisellä se on jo nimiössä, eikä samaa sanaa kirjoiteta kahdesti
    samalle riville.

    **Stackin pelaajamäärä on murtoluku eikä luku.** "4 pelaajaa" ei kerro
    poikkeamasta mitään ilman elossa olevien määrää: neljä viidestä on
    puolustuksen valinta, neljä neljästä on se mitä jäljellä oli. Sääntö
    laskee molemmat, joten rivikin sanoo molemmat.
    """
    text = f"kierros {entry.round_no}"
    if anomaly.rule != "ct_advance":
        text += f" ({ROUND_TYPE_FI.get(entry.round_type, entry.round_type)})"
    text += f": {_anomaly_points_text(entry)}"
    if entry.sources:
        # Suunnat vain crunchissa, ja **yhtäaikaisia** koska ne ovat saman
        # kierroksen havainto. Muilla tyhjä lista tarkoittaa "ei kysytty"
        # eikä "ei suuntia", joten sitä ei sanota ääneen.
        text += f", yhtä aikaa suunnista {_areas_text(entry.sources)}"
    # Kaksi demoa samalla kartalla: kierrosnumero ei yksilöi ilman
    # demotunnistetta. Tunniste on koodijaksona, koska se on tässä ainoa
    # käyttökelpoinen muoto -- sama peruste kuin kierrosliitteellä.
    #
    # Luku tulee KIERROKSISTA eikä orientaatiosta. Orientaatiosäännöillä ne
    # ovat sama luku (malli valvoo, että orientaatio kattaa täsmälleen ne
    # demot joilla poikkeama havaittiin), mutta stackilla orientaatiota ei
    # ole -- ja siitä luettuna tunniste jäisi pois juuri silloin, kun kartta
    # on kahdesta demosta ja lukija tarvitsee sen eniten.
    if len({round_entry.map_demo_id for round_entry in anomaly.rounds}) > 1:
        text += f" -- `{entry.map_demo_id}`"
    return text


def _anomaly_points_text(entry: AnomalyRound) -> str:
    """Kierroksen näytepisteet lukuineen.

    **Luku kuuluu hetkeensä.** Kun näytepisteillä on eri luvut, jokainen saa
    omansa (``5/5 pelaajaa 15 s ja 4/5 pelaajaa 30 s kohdalla``); kun luvut
    ovat samat, ne tiivistetään yhteen (``4/5 pelaajaa 15 ja 30 s kohdalla``).
    Tiivistäminen on turvallista vain siksi, että ehto vertaa **kaikkia**
    lukuja: aiemmin rivi latoi kierroksen maksimin jokaiselle näytepisteelle,
    ja mitattuna se väitti Infernon k2:sta viittä pelaajaa myös 30 s kohdalla,
    jossa heitä oli yksi.
    """
    counts = {(point.players, point.alive) for point in entry.points}
    if len(counts) == 1:
        players, alive = counts.pop()
        seconds = _seconds_list([point.sample_t_s for point in entry.points])
        return f"{_players_of(players, alive)} {seconds} s kohdalla"
    parts = [
        f"{_players_of(point.players, point.alive)} "
        f"{_seconds(point.sample_t_s)} s"
        for point in entry.points
    ]
    return f"{_join_fi(parts)} kohdalla"


def _players_of(players: int, alive: int | None) -> str:
    """``4 pelaajaa`` tai stackilla ``4/5 pelaajaa``.

    Elossa olevien määrä on **vain stackilla**, koska vain se laskee sen.
    Kahdella muulla säännöllä ``alive`` on ``None``, ja keksitty nimittäjä
    näyttäisi rivillä täsmälleen samalta kuin mitattu.
    """
    if alive is None:
        return players_text(players)
    return f"{players}/{alive} pelaajaa"


def _orientation_text(anomaly: Anomaly) -> str:
    """Alueen T-osuus ja sen oma otanta, demo kerrallaan.

    Osuus **ilman havaintomäärää** olisi vaarallisin luku koko rivillä: 1,00
    näyttäisi samalta yhdestä ja sadasta havainnosta. Kaksi demoa samalta
    kartalta voi antaa alueelle eri osuuden, ja silloin molemmat kirjoitetaan
    -- keskiarvo olisi luku, jota ei ole havaittu.

    Havaintomäärä on **ilman sulkeita**, koska koko lisätieto on jo väitteen
    suluissa: sisäkkäiset sulkeet pakottaisivat lukemaan rivin kahdesti.
    """
    parts = [
        f"T-osuus {_share(entry.t_share)} alueen "
        f"{entry.observations} havainnosta"
        for entry in anomaly.orientation
    ]
    return "; ".join(parts)


def _share(value: float) -> str:
    """Osuus kahdella desimaalilla ja suomalaisella pilkulla."""
    return f"{value:.2f}".replace(".", ",")


def _seconds_list(values: Sequence[float]) -> str:
    """``[15.0, 30.0] -> '15 ja 30'``."""
    return _join_fi([_seconds(value) for value in values])


def _areas_text(areas: Sequence[str]) -> str:
    """Aluenimet luettelona; calloutit pysyvät englanniksi."""
    return _join_fi([_area(name) for name in areas])


def _join_fi(parts: Sequence[str]) -> str:
    """``a, b ja c`` -- suomen kielen luettelo, ei pilkkujono.

    Viimeinen erotin on **ja** eikä pilkku, koska rivi luetaan lauseena:
    "suunnista Arch, TopofMid" näyttäisi katkaistulta luettelolta.
    """
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} ja {parts[-1]}"


def _no_anomalies_text(report: Report) -> str:
    """Teksti tyhjälle poikkeamaluvulle -- kattavuus mukaan luettuna.

    **"Ei poikkeamia" on havainto vain siitä, mitä tutkittiin.** Pelkkä
    lause ilman kattavuutta väittäisi mitattua negatiivista myös sokeasta
    pisteestä: luokittelemattomat kierrokset ovat rajattu ulos, orientaatio
    voi olla tyhjä, ja siteryhmät voivat jäädä saamatta. Jokainen sanotaan
    tässä ääneen, koska juuri se ero ("havainto eikä puute") on koko luvun
    arvo.

    Arkkitehtuurin (AD-10) **kaikki kolme sääntöä ajetaan** Story 2.14:stä
    lähtien, joten lause lykätyistä säännöistä latoutuu vain jos
    :data:`~pappascout.constants.ANOMALY_RULES_DEFERRED` täyttyy uudelleen.

    Menetelmä ja kynnykset ovat mukana samasta syystä: puhtaan raportin
    lukijalle on kerrottava mitä mitattiin ja millä rajoilla, eikä hän näe
    yhtäkään riviä, josta ne voisi päätellä.
    """
    scan = report.anomaly_scan
    rules = _join_fi(
        [ANOMALY_RULE_FI.get(name, name) for name in scan.rules]
    )
    parts = [
        f"Ei poikkeamia. Säännöt ({rules}) ajettiin "
        f"{scan.rounds_scanned} kierrokselle, mutta kaikki tutkivat vain "
        f"CT-puolen rivejä: crunch voi osua {scan.crunch_rounds} "
        f"kierroksella, CT-eteneminen {scan.advance_rounds} "
        "kierroksella, koska se on rajattu säästökierroksiin, ja stack "
        f"{scan.stack_rounds} kierroksella -- se lukee vain demot, joista "
        "siteryhmät saatiin johdettua."
    ]
    if scan.rules_deferred:
        parts.append(
            f"Arkkitehtuuri nimeää {len(scan.rules) + len(scan.rules_deferred)} "
            f"poikkeamasääntöä; näistä {len(scan.rules_deferred)} on "
            f"toteuttamatta ({', '.join(scan.rules_deferred)}), joten tämä "
            "luku ei kattavuudeltaan vastaa niitä."
        )
    if report.unclassified_rounds:
        parts.append(
            f"{report.unclassified_rounds} kierrosta jäi kokonaan tutkimatta, "
            "koska niiden kierrostyyppi puuttuu."
        )
    if scan.demos_without_orientation:
        parts.append(
            f"{demos_text(len(scan.demos_without_orientation))} ei antanut "
            "yhdellekään alueelle puoliorientaatiota, joten CT-etenemisen ja "
            "crunchin vaikeneminen niissä on sokea piste eikä havainto."
        )
    else:
        parts.append(
            "Jokainen demo antoi vähintään yhdelle alueelle "
            "puoliorientaation, joten sokeita pisteitä ei ole."
        )
    # Stackin sokea piste **ei ole täällä** vaan lukuohjeessa
    # (:func:`_stack_legend`). Kahdesta paikasta se latoutuisi tyhjään lukuun
    # kahdesti, ja lukuohje on se paikka, joka kirjoitetaan myös silloin kun
    # poikkeamia on -- eli juuri silloin, kun vaiennettu kartta on
    # näkymättömin.
    return " ".join(parts)


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


def _pruning_value(value: Any) -> str:
    """Yhden karsinta-asetuksen arvo yhteenvedon riville.

    Tyhjä lista on ``ei yhtään`` eikä tyhjä merkkijono: rivi ``skip_sample_
    seconds`` ilman arvoa lukisi kuin arvo olisi kadonnut matkalla. Sekunnit
    muotoillaan :func:`~pappascout.constants.seconds_label`illa, eli samalla
    tavalla kuin näytepisterivien nimiöt -- muuten yhteenveto ja runko
    puhuisivat samasta luvusta kahdella tavalla.
    """
    if isinstance(value, list):
        return "/".join(seconds_label(item) for item in value) or "ei yhtään"
    return _value(value)


def _pruning_summary_text(settings: ReportSettings) -> str | None:
    """Karsintasäännöt yhteenvedon riville, avain kerrallaan.

    **Mekaaninen luettelo eikä käsin kirjoitettu lause**: se syntyy osion
    kentistä, joten kuudes sääntö on rivillä heti kun se on osiossa. Käsin
    kirjoitettu lause jäisi jälkeen juuri silloin, kun sääntö lisätään.

    Muoto on sama kuin naapuririveillä (``Luokittelun kynnykset``,
    ``Aggregoinnin kynnykset``), ja peruste on niiden peruste: lukija arvioi
    väitettä sillä, miten se laskettiin -- ja karsinta päättää, mitkä
    väitteet hän näkee. Ilman riviä puhtaan raportin lukija ei voisi tietää,
    oliko jokin sääntö päällä, koska karsintakappaleet kirjoitetaan vain
    osuneista säännöistä.

    **``None`` kun jokainen sääntö on pois.** Silloin rivillä ei ole mitään
    ilmoitettavaa, ja sen kirjoittaminen rikkoisi tarinan tärkeimmän
    lupauksen: kaikki säännöt pois päältä tarkoittaa, että raportti on
    **merkki merkiltä** se, joka oli ennen Story 2.13:a -- eikä se, jossa on
    yksi rivi enemmän. Rivi ilmestyy siis täsmälleen silloin, kun karsinta on
    mukana päättämässä, mitä lukija näkee.

    Tunnistus on mekaaninen samasta syystä kuin luettelo: **jokaisen kentän
    epätosi arvo tarkoittaa "sääntö pois"** (``False``, tyhjä lista, ``0``),
    joten kuudes sääntö kelpaa tähän ilman muutosta. Ehto on kirjattu
    :class:`~pappascout.domain.models.ReportSettings`in docstringiin, koska se
    on vaatimus tulevalle kentälle.
    """
    values = settings.model_dump(mode="json")
    if not any(values.values()):
        return None
    return ", ".join(
        f"{key} {_pruning_value(values[key])}" for key in sorted(values)
    )


def _summary(
    report: Report, threshold: int | None, settings: ReportSettings
) -> list[SummaryItem]:
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
    pruning_text = _pruning_summary_text(settings)
    if pruning_text is not None:
        items.append(SummaryItem("Karsinnan säännöt", pruning_text))
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
        return UNKNOWN_MAP_LABEL.format(index=index)
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
    report: Report,
    *,
    settings: ReportSettings,
    round_list_paths: Sequence[str] = (),
) -> ReportView:
    """Rakenna raportista näkymämalli.

    Args:
        report: ``aggregate``-vaiheen tulos sellaisenaan.
        settings: ``[report]``-osio eli karsintasäännöt (Story 2.13).
            **Pakollinen eikä oletusarvoinen**: oletus, joka eroaisi
            ``settings.toml``ista, karsisi hiljaa eri tavalla kuin käyttäjän
            tiedosto sanoo -- ja oletus, joka on sama, olisi kopio
            asetustiedostosta koodissa. Karsinta koskee **esitystä eikä
            sisältöä**: jokainen karsittu arvo on yhä ``report.json``issa.
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
                views.append(
                    _round_type_view(entry, threshold, flags, settings)
                )
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
        # Nimi suojataan koodijaksona (``_identifier``), koska se on Story
        # 2.11:n jälkeen demon antamaa vapaata tekstiä: workshop-kartta
        # nimeltä ``*|Aim|* Botz [beta]`` on laillinen havainto, ja paljaana
        # se katkaisi otsikon kesken. Demon antamia merkkijonoja rungossa on
        # neljä -- joukkueen nimi, pelaajan nimi, kartan nimi ja **alueen
        # nimi** (:func:`_area`) -- ja kartan nimi oli kahdessa paikassa
        # kolmesta ilman suojausta.
        #
        # KOODIJAKSO EIKÄ ESCAPETUS, ja mekanismi on lainattu
        # ``_map_label``ista eikä valittu uudestaan: se perustelee saman
        # valinnan sanatarkasti juuri kartan nimelle, ja escapetus tuottaisi
        # kartalle **toisen kirjoitusasun** kuin jäljitettävyysluvun rivillä.
        # Raportti luetaan myös raakana, ja sama kartta kahdella
        # kirjoitusasulla lukisi kahtena karttana.
        heading = (
            f"{_identifier(map_report.map_name)} -- "
            f"{rounds_text(map_report.sample.rounds)}, "
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

    anomaly_views, dropped_note = _anomaly_views(report)
    return ReportView(
        title=_title(report),
        summary=tuple(_summary(report, threshold, settings)),
        anomalies=anomaly_views,
        anomalies_note=None if anomaly_views else _no_anomalies_text(report),
        anomalies_dropped_note=dropped_note,
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
        legend=tuple(_legend(flags, report, settings)),
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


def _legend(
    flags: _Flags, report: Report, settings: ReportSettings
) -> list[str]:
    """Selitykset, jotka kirjoitetaan kerran raportin loppuun.

    ``report`` on argumenttina siksi, että poikkeamasääntöjen selitys nimeää
    **kynnykset, joilla ne ajettiin**. Ne luetaan raportista eikä keksitä
    täällä -- sama sääntö kuin kuvion kynnyksellä
    (:func:`pattern_min_rounds`): säädetty ``settings.toml`` näkyy raportin
    tekstissä vain, jos teksti tulee raportista.

    ``settings`` on argumenttina karsintasääntöjen takia (Story 2.13), ja se
    on eri lähde eri syystä: karsintaraja ei ole ``report.json``issa eikä
    kuulukaan sinne -- se on esitysvalinta, joka tehdään renderöinnissä, kun
    kynnys on aggregoinnin lukuun vaikuttava arvo. Rajan luku on silti
    kirjoitettava lukuohjeeseen, koska rivin perässä oleva "3 harvinaisempaa
    aluetta jäi pois" ei kerro, montako jäi.
    """
    notes: list[str] = []
    notes.append(
        "Jokainen väite kantaa otantansa muodossa (n/m kierroksesta): n on "
        "kierrokset, joissa havainto tehtiin, m kyseisen kierrostyypin kaikki "
        "kierrokset. Mediaanin otanta rivin otsikossa (esimerkiksi "
        "\"mediaani 14,2 s, 7/9 kierroksesta\") noudattaa tätä sääntöä: se "
        "kertoo, monellako kierroksella ajoitus mitattiin. Saman rivin "
        "aluevaateet laskevat sen sijaan vain niitä kierroksia, joilla "
        "havainto oli olemassa, joten niiden nimittäjä on pienempi."
    )
    notes.append(
        "Ensikontaktin rivi kertoo elossa olevat pelaajat alueittain sillä "
        "hetkellä, kun kierroksen ensimmäinen ristiinpuolinen osuma tapahtui."
    )
    notes.extend(_anomaly_legend(report))
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
    notes.extend(_pruning_legend(flags, settings))
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


def _protected_round_types_text() -> str:
    """Suojatut kierrostyypit lukuohjeen lauseeksi.

    Lause on **johdettu** :data:`PROTECTED_ROUND_TYPES`ista eikä kirjoitettu
    käsin, ja se liitetään jokaiseen karsintakappaleeseen. Kaksi syytä:

    1. Kappale, joka sanoo "kirjoitetaan kaksi yleisintä kohdetta", on
       ehdoton lause, ja **samassa raportissa** suojatun kierrostyypin lohko
       tulostaa niitä neljä. Ilman poikkeuslausetta lukuohje väittää
       raportista enemmän kuin raportti tekee.
    2. Kun luetteloon tulee kuudes tyyppi, jokainen kappale kertoo sen
       itsestään -- käsin kirjoitettuina yksi niistä jäisi jälkeen.

    Järjestys tulee :data:`ROUND_TYPE_ORDER`ista, jotta lause on sama ajosta
    toiseen (``frozenset`` ei ole järjestetty).
    """
    names = _join_fi(
        [
            ROUND_TYPE_FI.get(name, name)
            for name in ROUND_TYPE_ORDER
            if name in PROTECTED_ROUND_TYPES
        ]
    )
    return f"Karsinta ei koske näitä kierrostyyppejä: {names}."


def _pruning_legend(flags: _Flags, settings: ReportSettings) -> list[str]:
    """Karsintasääntöjen selitykset: **mitä puuttuva rivi tarkoittaa**.

    Kappale kirjoitetaan vain siitä säännöstä, joka **oikeasti karsi
    jotakin** tässä raportissa -- ei jokaisesta päällä olevasta säännöstä.
    Ero on olennainen ja se on sama sääntö kuin muualla tässä funktiossa
    (:func:`_player_counter_legend`, ``flags.kills_shown``): lukuohje selittää
    sen, mitä raportissa on tai mitä siitä puuttuu, eikä sitä mitä koodi osaa
    tehdä. Selitys säännöstä, joka ei osunut kertaakaan, kertoisi lukijalle
    puuttuvasta rivistä, jota ei ole -- eli olisi väite raportista, joka ei
    pidä.

    Sama vaatimus koskee **rajauksia**: jokainen kappale kertoo, ettei
    karsinta koske suojattuja kierrostyyppejä
    (:func:`_protected_round_types_text`), koska niiden lohkot ovat samassa
    raportissa karsimattomina.

    Jokainen kappale nimeää myös **asetuksen**, jolla sääntö käännetään pois.
    Karsinta on Veetin säädettävissä ilman koodimuutosta, eikä se ole
    säädettävissä, jos raportti ei kerro minkä nimistä arvoa säädetään.
    """
    notes: list[str] = []
    exception = _protected_round_types_text()
    if flags.saturated_dropped:
        notes.append(
            "**Kylläinen kalustorivi on jätetty pois.** Kun jakaumassa on "
            f"vain arvo {PLAYERS_ON_SERVER} ja havainto saatiin joka "
            "kierrokselta, rivi sanoo että kaikilla viidellä oli panssari "
            "(tai ase) joka kierroksella -- se on odotus eikä havainto. Luku "
            "on yhä report.jsonissa. **Kylläisyys ei ole ainoa syy, jonka "
            "takia kalustorivi voi puuttua**: täydellä ostolla myös "
            "toistumisen kynnys voi pudottaa sen, ja silloin lohkon oma "
            f"huomautus kertoo siitä. {exception} Asetus: "
            "[report].drop_saturated_equipment_lines."
        )
    if flags.equipment_merged:
        notes.append(
            "**Aseistettujen ja panssaroitujen rivi on kirjoitettu yhtenä** "
            "silloin, kun jakaumat ovat identtiset: luvut on luettu samalta "
            "tickiltä samasta pelaajajoukosta ja samalla jakajalla, joten "
            "toinen rivi ei kertoisi mitään uutta. Kaksi erillistä riviä "
            f"tarkoittaa siis, että luvut eroavat -- ja se ero on havainto. "
            f"{exception} Asetus: [report].merge_equal_equipment_lines."
        )
    if flags.skipped_samples:
        samples = _join_fi([f"{value} s" for value in flags.skipped_samples])
        notes.append(
            f"**Näytepistettä {samples} ei kirjoiteta tähän raporttiin.** "
            "Puuttuva näytepiste ei tarkoita puuttuvaa havaintoa: se on "
            "report.jsonissa ja parsituissa tauluissa sellaisenaan, eikä "
            "[parse].snapshot_seconds ole muuttunut -- kyse on vain siitä, "
            "tulostetaanko rivi. Myöhäinen näytepiste kertoo eloonjääneistä "
            f"eikä asetelmasta. {exception} Asetus: "
            "[report].skip_sample_seconds."
        )
    if flags.utility_targets_capped:
        notes.append(
            "Utilityn kohderiviltä kirjoitetaan "
            f"{settings.max_utility_targets} yleisintä **kohdetta** (sama "
            "kohde voi olla rivillä useammin kuin kerran: eri heittoalueelta "
            "tai eri aikaikkunassa), ja rivin perässä on niiden kohteiden "
            "määrä, jotka jäivät pois; rivi, jolla on kohteita viidestä "
            "yhdeksään, on luettelo eikä kuvio. Jokainen kohde on yhä "
            f"report.jsonissa kentässä utility. {exception} Asetus: "
            "[report].max_utility_targets."
        )
    if flags.kill_areas_capped:
        notes.append(
            f"Tapporiviltä kirjoitetaan {settings.max_kill_areas} yleisintä "
            "aluetta samalla säännöllä, ja pois jääneiden määrä on rivin "
            "perässä. Yhtä yleiset alueet säilyvät molemmat, joten rivillä "
            "voi olla rajaa enemmän alueita. Jokainen alue on yhä "
            f"report.jsonissa kentässä deaths.kills. {exception} Asetus: "
            "[report].max_kill_areas."
        )
    return notes


def _anomaly_legend(report: Report) -> list[str]:
    """Poikkeamaluvun selitykset: menetelmä ja **jokaisen säännön ehdot**.

    Kolme kappaletta niistä säännöistä, jotka on toteutettu, ja ne
    kirjoitetaan **myös tyhjään lukuun**: orientaation menetelmä sekä
    etenemisen ja crunchin ehdot. :func:`_stack_legend` lisää kaksi
    (stackin ehdot ja sen kattavuus), joten nykyisellä sääntöjoukolla
    kappaleita on viisi -- mutta luku seuraa :data:`ANOMALY_RULES`ia eikä ole
    vakio: jos stack joskus palaa lykättyjen luetteloon, sen kaksi kappaletta
    jäävät pois eikä lukuohje selitä riviä, jota ei voi olla.
    Peruste on eri kuin muilla lukuohjeen kappaleilla: nämä eivät selitä
    riviä, joka raportissa on, vaan **mitä mitattiin**. Puhtaan raportin
    lukija tarvitsee ne enemmän kuin kukaan muu: hän näkee vain väitteen
    "ei poikkeamia" eikä yhtäkään riviä, josta menetelmän voisi päätellä.

    Sääntöjen epäsymmetria sanotaan ääneen, koska se on näkymätön muuten:
    eteneminen on rajattu säästökierroksiin, crunch ja stack eivät ole, joten
    sama alue voi esiintyä ``eco``-rivillä muttei ``default``-rivillä -- ja
    ilman selitystä se näyttää puuttuvalta havainnolta.
    """
    share = _threshold_float(report, "advance_t_share")
    observations = _threshold_int(report, "advance_area_min_observations")
    bound = _threshold_float(report, "advance_max_sample_s")
    advance_players = _threshold_int(report, "advance_min_players")
    crunch_players = _threshold_int(report, "crunch_min_players")
    crunch_sources = _threshold_int(report, "crunch_min_sources")

    orientation = (
        f"Luvun {ANOMALY_HEADING} T-osuus on **demon oma havainto** siitä, "
        "kumman puolen aluetta alue on: se on alueen elossa-havainnoista "
        "aikanäytepisteillä laskettu T-puolen osuus, **molempien joukkueiden** "
        "riveistä. Ei karttatietokantaa eikä käsin annettua aluejakoa -- ja "
        "eri demo voi antaa samalle alueelle eri osuuden, joten havaintomäärä "
        "on osuuden vieressä."
    )
    if share is not None and observations is not None:
        orientation += (
            f" Alue on T:n aluetta, kun osuus on vähintään {_share(share)} ja "
            f"alueella on vähintään {observations} havaintoa; sitä vähemmällä "
            "alue ei ole kummankaan puolen aluetta eikä tuota poikkeamaa."
        )
    notes = [orientation]

    advance = (
        f"**{ANOMALY_RULE_FI['ct_advance']}**: subjektin CT-pelaaja alueella, "
        "joka on siinä demossa T:n hallussa, **säästökierroksella** (eco, "
        "force tai puoliosto)."
    )
    if advance_players is not None and bound is not None:
        advance += (
            f" Vähintään {players_text(advance_players)} alueella ja havainto "
            f"enintään {_seconds(bound)} sekunnin kohdalla kierroksen alusta."
        )
    notes.append(advance)

    crunch = (
        f"**{ANOMALY_RULE_FI['crunch']}**: sama T:n alue, mutta pelaajien on "
        "**saavuttava** sinne yhtä aikaa eri suunnista -- lähtösuunta on "
        "pelaajan oma alue edellisellä näytepisteellä."
    )
    if crunch_players is not None and crunch_sources is not None:
        crunch += (
            f" Vähintään {players_text(crunch_players)} ja "
            f"{crunch_sources} eri suuntaa."
        )
    crunch += (
        " **Crunchia ei ole rajattu kierrostyyppiin**, toisin kuin etenemistä, "
        "joten sen otanta on puolen kaikki kierrokset ja nimiö kertoo millä "
        "kierrostyypeillä se havaittiin. Sama kierros voi siis tuottaa "
        "molemmat rivit, ja täysi osto vain crunchin."
    )
    notes.append(crunch)
    notes.extend(_stack_legend(report))
    return notes


def _stack_legend(report: Report) -> list[str]:
    """Stackin kaksi kappaletta: mitä sääntö on ja mitä se ei nähnyt.

    **Kattavuus on täällä eikä vain tyhjän luvun tekstissä.** Nuken kaltainen
    kartta vaikenee myös silloin, kun muilla kartoilla on osumia -- ja juuri
    silloin lukija näkee luvun, jossa Nukea ei ole, ilman mitään mikä
    kertoisi miksi. Tyhjän luvun teksti (:func:`_no_anomalies_text`) ei
    silloin ladota lainkaan.

    Ryhmä sanotaan **johdetuksi** eikä annetuksi, koska se on koko säännön
    peruste: aluejakoa ei ole missään tietokannassa, vaan se lasketaan sen
    demon omasta pistepilvestä joka kerta.
    """
    scan = report.anomaly_scan
    if "stack" not in scan.rules:
        # Sääntöä ei ajettu, joten sen selittäminen kuvaisi rivejä, joita ei
        # voi olla. Kattavuuden kertoo silloin rules_deferred.
        return []
    players = _threshold_int(report, "stack_min_players")
    margin = _threshold_float(report, "stack_group_margin")

    rule = (
        f"**{ANOMALY_RULE_FI['stack']}**: subjektin puolustus kasautuneena "
        "yhden siten ympärille. Alueryhmä on **johdettu tästä demosta**: "
        "jokaisen alueen keskipiste lasketaan demon omasta pistepilvestä, ja "
        "alue kuuluu lähemmän siten ryhmään"
    )
    if margin is not None:
        rule += f", jos toinen site on vähintään {_ratio(margin)} kertaa kauempana"
    rule += (
        ". Ei karttatietokantaa eikä käsin annettua aluejakoa. Osuma vaatii "
    )
    if players is not None:
        rule += f"vähintään {players_text(players)} saman siten ryhmässä ja "
    rule += (
        "vähintään yhden heistä sitellä itsellään; spawnissa seisova ei "
        "laske. Rivin luku on muotoa 4/5 -- ryhmässä olleet kaikista elossa "
        "olleista. **Stackia ei ole rajattu kierrostyyppiin** eikä se lue "
        "alueen T-osuutta, joten se ei ole kummankaan toisen säännön tiukempi "
        "eikä löysempi muoto."
    )
    notes = [rule]

    # Otanta samassa muodossa kuin raportin väitteillä (n/m), koska luku on
    # sama asia: montako kierrosta sääntö näki niistä, joilla se voisi osua.
    # Sanamuoto "N kierrosta M:sta" taipuisi väärin luvulla 1.
    coverage = (
        f"Stackin kattavuus on {scan.stack_rounds}/{scan.crunch_rounds} "
        "CT-kierroksesta."
    )
    if scan.demos_without_site_groups:
        coverage += (
            f" Erotus on {demos_text(len(scan.demos_without_site_groups))} "
            "ilman siteryhmiä: kartalla, jolla A ja B ovat päällekkäin eri "
            "kerroksissa (Nuke), etäisyys siteeseen ei kerro kummasta "
            "puolesta on kyse, eikä jakoa saa keksiä. **Sääntö vaikenee "
            "siellä**, ja vaikeneminen on oikea vastaus -- muttei havainto "
            "siitä, ettei stackeja ollut."
        )
    else:
        coverage += " Jokaiselta demolta saatiin siteryhmät."
    notes.append(coverage)
    return notes


def _ratio(value: float) -> str:
    """Suhdeluku suomalaisella desimaalipilkulla ilman turhia nollia.

    ``1.25 -> '1,25'``, ``2.0 -> '2'``. Eri kuin :func:`_share`, joka pakottaa
    kaksi desimaalia: osuus 1,0 on eri väite kuin 1, mutta marginaali 2,00 ei
    ole tarkempi kuin 2.
    """
    return f"{value:g}".replace(".", ",")


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
