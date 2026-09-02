"""``Report`` -- ``aggregate``-vaiheen tulos ja ``render``-vaiheen ainoa syöte.

Tämä moduuli on **jaettu sopimus** täsmälleen samassa mielessä kuin
:mod:`pappascout.domain.schemas` on tauluille: ``aggregate`` (Story 2.3)
kirjoittaa mallin JSONiksi tiedostoon ``aggregates/<team_key>/report.json``, ja
``render`` (Story 2.4) lukee sen. **``render`` ei laske mitään** -- jokainen
raportissa esiintyvä luku on täällä valmiina, ja uusi luku raporttiin tarkoittaa
muutosta **tähän malliin**, ei Jinja-raporttimalliin.

Otanta on rakenteessa, ei kommentissa
-------------------------------------
Jokainen väite kantaa otantansa. Kaksi lukua, joilla on täsmälleen yksi
tulkinta:

``n``
    Kierrokset, joissa havainto tehtiin.
``m``
    Kyseisen puolen ja kierrostyypin kaikki kierrokset, joista havainto **oli
    ylipäätään luettavissa**.

Alueen jakauma :class:`AreaDistribution` sisältää myös arvon ``players = 0``
(alue oli tyhjä), joten ``n``-arvojen summa yhden alueen yli on aina ``m``.
Se ei ole koriste vaan tarkistus: jos summa ei täsmää, jokin kierros katosi
liitoksessa. Malli valvoo sen itse (:meth:`AreaDistribution._check_sample`), eli
epäkelpoa raporttia ei voi edes rakentaa muistiin. Sama tarkistus tehdään
**tasojen välillä**: kierrostyyppien summa on puolen otanta, puolien summa
kartan ja karttojen summa koko raportin -- juuri siellä kadonnut kierros
näkyisi ensimmäisenä, eikä yksikään lehti huomaisi mitään.

Yksi kenttä on tarkoituksella sääntöä lukuun ottamatta: :class:`FirstContactArea`
laskee läsnäoloa eikä pelaajamäärää, joten sama kierros tuottaa havainnon
jokaiselle alueelle, jolla joukkueella oli pelaaja. Täysi jakauma samalta
hetkeltä on ``positions``-listan ``first_contact``-näytepisteessä.

Yhdessä paikassa ``m`` **ei ole kierroksia**: :class:`KillArea` laskee tappoja,
joten sen ``Σ n = tappojen määrä``. Kierrostyypillä voi olla enemmän tappoja
kuin kierroksia, joten "n/m kierroksesta" olisi siellä suoraan väärä lause --
ja :class:`DeathReport`in dokumentaatio sanoo sen ääneen, koska raportti
muotoilee juuri sen rivin eri yksiköllä.

Kolme lokeroa, ei kahta
-----------------------
``is_league`` syntyy vasta ``select``-vaiheessa (Epic 3), joten käsin tuoduilla
demoilla se on ``null``. Kahden lokeron jako (``league`` / ``other``) pakottaisi
valitsemaan kahdesta valheesta: merkitä käsin tuodut liigaotteluiksi tai muiksi.
Otanta on siksi ``{league, other, unknown}`` jokaisella tasolla
(:class:`Sample`), ja kolmas lokero sanoo mitä tiedetään.

Kaikki lasketaan, raportti valitsee
-----------------------------------
Malli sisältää **kaikki** kierrostyypit, myös täydet ostot ja jatkoajan.
Säästökierrosten ja defaultin eri käsittely on esitysvalinta, ja se kuuluu
``render``-vaiheeseen: jos aggregointi suodattaisi, valinnan muuttaminen
vaatisi uudelleenlaskennan ja ``report.json`` lakkaisi olemasta täysi kuva
siitä, mitä demoista tiedetään.

Poikkeamat ovat raportin juuressa eivätkä kierrostyypin alla
------------------------------------------------------------
``anomalies`` on :class:`Report`in kenttä, ei :class:`RoundTypeReport`in.
Poikkeama on epicin arvokkain tuotos, ja kierrostyypin alla se olisi
hajallaan 24 lohkossa -- juuri se ongelma, jonka Story 2.5 ratkaisee. Siksi
jokainen :class:`Anomaly` kantaa itse kartan, puolen ja kierrostyypit: se on
luettavissa yhtenä lukuna ilman että lukija etsii sen paikkaa puusta.

**Nimittäjä on sääntökohtainen, ja se on tarkoitus.** ``ct_advance`` on
säästökierrosten ilmiö, joten kierrostyyppi on osa havaintoa ja ``m`` on sen
kierrostyypin kierrokset kartalla ja puolella. ``crunch`` ei tunne
kierrostyyppiä lainkaan, joten sen ryhmittely kierrostyypin mukaan hajottaisi
saman kuvion eco-riviksi ja default-riviksi eri jakajilla -- eli toistaisi
luvun sisällä juuri sen hajanaisuuden, jonka poistamiseksi luku tehtiin.
Crunchin ``m`` on siksi puolen **kaikki** kierrokset kartalla, ja
``round_types`` kertoo millä tyypeillä se havaittiin.

Rakenne ei valvo nimittäjää ristiin puuta vasten (poikkeamat eivät ole puun
lehtiä), joten yhteys on aggregoinnin vastuulla. :class:`Anomaly` valvoo sen
sijaan **sisäisen** ristiriidattomuutensa: ``n`` on kierroslistan pituus,
``players_max`` sen suurin havainto ja ``round_types`` sen tyyppijoukko, joten
yhteenveto ei voi olla eri mieltä kuin rivit joista se on koottu.

Rivi ei väitä yhtäaikaisuutta yli kierrosrajan
----------------------------------------------
:class:`AnomalyRound` on omana solmunaan siksi, että crunchin **lähtösuunnat
ovat yhtäaikaisia vain saman kierroksen sisällä**. Kahden kierroksen
suuntien yhdiste ("suunnista A, B, C ja D") lukisi neljäksi yhtäaikaiseksi
suunnaksi, mikä on päinvastoin kuin määritelmä. Sama koskee näytepisteitä ja
pelaajamäärää. Kierrosnumero on samassa solmussa, koska scoutin seuraava teko
on katsoa se kierros demolta -- ja luku on turha, jos se kertoo että jotain
tapahtui muttei missä sen näkee.

Mitä täällä **ei** ole: tulkintoja. Sanoja "fake" tai "rush" ei esiinny
missään kentässä -- vain havaintoja ja lukumääriä. Poikkeavat asetelmat
(``anomalies``) ovat Story 2.5:n lisäys tähän samaan malliin.
"""

from __future__ import annotations

import re
from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pappascout.constants import (
    ANOMALY_RULES,
    ROUND_TYPES,
    SAMPLE_BUCKETS,
    AnomalyRule,
    AreaSource,
    RoundType,
    SampleKind,
    Side,
)
from pappascout.errors import AggregateError

__all__ = [
    "REPORT_SCHEMA_VERSION",
    "slugify",
    "team_slug",
    "SLUG_FALLBACK",
    "SampleBucket",
    "Sample",
    "PlayersCount",
    "AreaDistribution",
    "Position",
    "UtilityUse",
    "GrenadeCount",
    "UtilityCounts",
    "ArmedCount",
    "ArmedPlayers",
    "ArmoredCount",
    "ArmoredPlayers",
    "FirstContactArea",
    "FirstDeathArea",
    "KillArea",
    "DeathReport",
    "RoundTypeReport",
    "SideReport",
    "MapReport",
    "RosterEntry",
    "TeamReport",
    "MissingDemo",
    "AreaOrientation",
    "AnomalyRound",
    "Anomaly",
    "AnomalyScan",
    "MAP_NAME_SOURCES",
    "MapNameSource",
    "Report",
]

#: Raporttimallin skeemaversio. Nostetaan, kun rakenne muuttuu niin, ettei
#: vanha ``report.json`` enää validoidu -- silloin ``render`` kertoo, että
#: aggregointi on ajettava uudelleen, sen sijaan että se muotoilisi puolikkaan
#: raportin hiljaa.
#:
#: **5.0.0 (Story 2.9): rakenne ei muuttunut, mutta arvojoukko muuttui.**
#: ``AreaSource``-luettelosta poistui ``snapped`` ja tilalle tuli
#: ``point_cloud``, joten vanha ``report.json`` ei enää validoidu -- yksikään
#: kenttä ei kadonnut, mutta ``UtilityUse.area_source`` hylkää vanhan arvon.
#: Versio nousee siksi täsmälleen samasta syystä kuin puuttuvasta kentästä:
#: ehto on "validoituuko vanha tiedosto", ei "tuliko uusi kenttä".
#:
#: **6.0.0 (Story 2.11): sama sääntö, sama syy.** ``MapReport.map_name_source``
#: sai uuden arvon ``demo_header``, joten **uusi** ``report.json`` ei validoidu
#: vanhaa mallia vasten -- ja vanhan tiedoston kartat on joka tapauksessa
#: ryhmitelty eri säännöllä kuin tämän version, koska nimi luetaan nyt demon
#: otsikosta. Kaksi FACEIT-demoa samalta kartalta on vanhassa tiedostossa kaksi
#: haaraa ja uudessa yksi; sama rakenne, eri luvut. Sitä ei saa muotoilla
#: hiljaa tämän ajon tulokseksi.
#:
#: **7.0.0 (Story 2.5): uusi kenttä, jolla on oletus -- ja versio nousee
#: silti.** ``Report.anomalies`` on ``default_factory=list``, joten vanha
#: ``report.json`` validoituisi tyhjällä listalla. Juuri se on syy nostaa:
#: tyhjä poikkeamaluku on tässä mallissa **havainto** ("ei poikkeamia"), joten
#: vanhasta tiedostosta renderöity raportti väittäisi mitatuksi tulokseksi
#: sen, ettei sääntöjä ollut olemassa. Ehto "validoituuko vanha tiedosto" ei
#: siis riitä yksin: myös oletusarvo, joka on erotettavissa havainnosta,
#: nostaa version.
REPORT_SCHEMA_VERSION = "7.0.0"


#: Merkit, jotka eivät kelpaa tiedostonimeen. Slug on ASCII-osajoukko, koska
#: arkisto on OneDrivessa ja kahden koneen yhteinen.
_NON_WORD = re.compile(r"[^a-z0-9]+")

#: Slug, jota käytetään kun nimestä ei jää mitään jäljelle. Se on **jaettu
#: vakio**, joten se ei yksilöi mitään -- kaksi joukkuetta saisi saman
#: tiedostonimen. Käytä sitä vain viimeisenä keinona, kun edes tunnisteesta ei
#: saada slugia.
SLUG_FALLBACK = "joukkue"


def slugify(text: str) -> str:
    """Tiedostonimeen kelpaava muoto, tai **tyhjä merkkijono**.

    Tyhjä paluuarvo on tarkoituksellinen ja se erottaa tämän funktion
    :func:`team_slug`istä: kyrillinen tai CJK-nimi ei jätä jäljelle yhtään
    ASCII-merkkiä, ja silloin kutsujan on voitava valita **oma** varapolkunsa.
    Jaettu vakio antaisi jokaiselle tällaiselle joukkueelle saman
    tiedostonimen.
    """
    return _NON_WORD.sub("-", text.lower()).strip("-")


def team_slug(team_key: str) -> str:
    """Tiedostonimeen kelpaava muoto joukkueen tunnisteesta.

    ``render`` nimeää raportin ``<aika>-<team_slug>.md``, joten slug ei saa
    sisältää polkuerottimia eikä ääkkösiä.

    Varapolku on :data:`SLUG_FALLBACK`, joka **ei yksilöi mitään**. Kun
    kutsujalla on toinen ehdokas (esimerkiksi tunniste nimen rinnalla), käytä
    :func:`slugify`ä ja valitse varapolku itse.
    """
    return slugify(team_key) or SLUG_FALLBACK


def _check_rounds_add_up(
    total: "Sample", parts: "list[Sample]", level: str, child: str
) -> None:
    """Tarkista, että ylätason otanta on alatasojen summa.

    ``Σ n = m`` valvotaan lehdissä, mutta juuri **tasojen välissä** kadonnut
    kierros näkyisi ensimmäisenä: liitos ``(map_demo_id, round_no)`` voi
    pudottaa rivin, jolloin kierrostyyppien summa jää puolen otantaa
    pienemmäksi eikä yksikään lehti huomaa mitään. Vertailu tehdään sekä
    yhteissummasta että **jokaisesta lokerosta erikseen**, koska kierros voisi
    muuten vaihtaa lokeroa summan muuttumatta.

    Demoja ei summata: sama demo tuottaa kierroksia molemmille puolille ja
    useaan kierrostyyppiin, joten alatasojen demomäärien summa on suurempi
    kuin ylätason. Karttataso on ainoa poikkeus, ja se tarkistetaan siellä
    erikseen.

    Raises:
        AggregateError: Jos summa ei täsmää. Viesti nimeää tason ja lokeron.
    """
    # Lokerot luetaan samasta luettelosta kuin muualla: kaksi kopiota
    # erkanisivat, ja silloin uusi lokero jäisi tarkistamatta.
    for bucket in (None, *SAMPLE_BUCKETS):
        got = (
            total.rounds
            if bucket is None
            else getattr(total, bucket).rounds
        )
        parts_sum = sum(
            (p.rounds if bucket is None else getattr(p, bucket).rounds)
            for p in parts
        )
        if got != parts_sum:
            where = "yhteensä" if bucket is None else f"lokerossa {bucket}"
            raise AggregateError(
                f"Otanta ei täsmää tasolla {level}: {child}-tasojen "
                f"kierrosten summa on {parts_sum} {where}, mutta {level} "
                f"väittää otannakseen {got}.\n"
                "Ero tarkoittaa, että kierros katosi tasojen välissä -- "
                "yleensä liitoksessa (map_demo_id, round_no)."
            )


class _Node(BaseModel):
    """Raporttimallin kantaluokka: tuntematon kenttä on virhe, ei ohitus.

    ``frozen`` siksi, että malli on sopimus eikä työtila: ``render`` ei saa
    korjailla lukuja lukiessaan niitä.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class SampleBucket(_Node):
    """Yhden otantalokeron demo- ja kierrosmäärä."""

    demos: int = Field(ge=0)
    rounds: int = Field(ge=0)


class Sample(_Node):
    """Otanta yhdellä tasolla kolmessa lokerossa.

    ``unknown`` on lokero demoille, joiden ``is_league`` on tyhjä. Se ei ole
    virhetila vaan tavallisin tila ennen Epic 3:a: ihminen tietää haetun demon
    lajin, mutta demossa itsessään ei ole sitä tietoa, eikä ``aggregate``
    arvaa.

    ``demos`` ja ``rounds`` ovat lokeroiden summat valmiiksi laskettuina, jotta
    ``render`` ei laske niitä.
    """

    demos: int = Field(ge=0)
    rounds: int = Field(ge=0)
    league: SampleBucket
    other: SampleBucket
    unknown: SampleBucket

    @model_validator(mode="after")
    def _check_totals(self) -> Sample:
        buckets = (self.league, self.other, self.unknown)
        demos = sum(b.demos for b in buckets)
        rounds = sum(b.rounds for b in buckets)
        if self.demos != demos or self.rounds != rounds:
            raise AggregateError(
                "Otannan summat eivät täsmää lokeroihin: "
                f"demos={self.demos} (lokerot {demos}), "
                f"rounds={self.rounds} (lokerot {rounds}). "
                "Jokainen demo kuuluu täsmälleen yhteen lokeroon, joten "
                "summan on oltava lokeroiden summa."
            )
        return self


class PlayersCount(_Node):
    """Yksi pylväs alueen pelaajamääräjakaumassa.

    ``players`` on **elossa olevien** pelaajien määrä alueella näytepisteessä
    -- kuollut pelaaja ei tuota riviä alueelle. ``n`` on niiden kierrosten
    määrä, joissa alueella oli täsmälleen tämä määrä.
    """

    players: int = Field(ge=0)
    n: int = Field(gt=0)


class AreaDistribution(_Node):
    """Yhden alueen pelaajamääräjakauma yhdessä näytepisteessä.

    Tämä on se rakenne, josta tavoiteanalyysin rivi *"3A ja 2B"* luetaan:
    alue ``BombsiteA``, ``players = 3``, ``n`` kierrosta ``m``:stä.

    Jakauma sisältää myös arvon ``players = 0``, joten ``n``-arvojen summa on
    aina ``m``. Pylväitä, joiden ``n`` on nolla, ei kirjoiteta -- ne
    väittäisivät havainnoksi sen, ettei havaintoa ole.
    """

    #: Pelin oma ``env_cs_place``-alue. ``null`` = pelaajan aluetta ei saatu;
    #: rivi ei katoa, koska tuntematon sijainti on eri asia kuin tyhjä alue.
    area: str | None
    m: int = Field(ge=0)
    players_dist: list[PlayersCount]

    @model_validator(mode="after")
    def _check_sample(self) -> AreaDistribution:
        """``Σ n = m``. Ilman tätä luku ei tarkoita mitään.

        Raises:
            AggregateError: Jos summa ei täsmää. Poikkeus on tarkoituksella
                :class:`~pappascout.errors.AggregateError` eikä pelkkä
                ``ValueError``: kyse ei ole muotoiluvirheestä vaan siitä, että
                kierros katosi liitoksessa.
        """
        total = sum(p.n for p in self.players_dist)
        if total != self.m:
            raise AggregateError(
                f"Otanta ei täsmää alueella {self.area!r}: pelaajamäärien "
                f"n-arvojen summa on {total}, mutta kierroksia on {self.m}.\n"
                "Jokaisen kierroksen on tuotettava alueelle täsmälleen yksi "
                "havainto -- myös silloin, kun alue oli tyhjä (players = 0). "
                "Ero tarkoittaa, että kierros katosi liitoksessa "
                "(map_demo_id, round_no) tai että jakaumasta puuttuu "
                "nollalokero."
            )
        seen = [p.players for p in self.players_dist]
        if len(seen) != len(set(seen)):
            raise ValueError(
                f"Alueen {self.area!r} jakaumassa on sama pelaajamäärä "
                "kahdesti; jakauman on oltava pylväs per pelaajamäärä."
            )
        return self


class Position(_Node):
    """Yksi näytepiste: kaikkien alueiden jakaumat samalta hetkeltä.

    Näytepisteitä on kahta lajia, ja ``sample_kind`` erottaa ne:

    ``time``
        ``[parse].snapshot_seconds`` -luku sellaisenaan (6, 15, 30, 45 s), sama
        joka kierroksella ja siksi vertailukelpoinen. ``seconds`` on se luku.
    ``first_contact``
        Kierroksen ensimmäinen ristiinpuolinen osuma. Hetki on eri joka
        kierroksella, joten ``seconds`` on ``null`` ja ``seconds_median``
        kertoo mitatun ajoituksen.

    ``m`` on niiden kierrosten määrä, joilla **tämä näytepiste on olemassa**,
    ei kierrostyypin kaikkien kierrosten määrä. Ne eroavat: 45 sekunnin näyte
    puuttuu kierrokselta, joka ratkesi 30 sekunnissa. ``rounds_missing``
    kertoo erotuksen, jotta kierros ei katoa hiljaa.
    """

    sample_kind: SampleKind
    seconds: float | None
    seconds_median: float | None = None
    m: int = Field(ge=0)
    rounds_missing: int = Field(ge=0)
    areas: list[AreaDistribution]

    @model_validator(mode="after")
    def _check_areas_share_the_sample(self) -> Position:
        for area in self.areas:
            if area.m != self.m:
                raise AggregateError(
                    f"Näytepisteen {self.seconds!r} alue {area.area!r} väittää "
                    f"otannakseen {area.m}, mutta näytepisteellä on {self.m} "
                    "kierrosta. Saman näytepisteen kaikkien alueiden on "
                    "jaettava sama otanta -- muuten kaksi lukua samasta "
                    "hetkestä eivät ole vertailukelpoisia."
                )
        return self

    @model_validator(mode="after")
    def _check_seconds_matches_kind(self) -> Position:
        if self.sample_kind == "time" and self.seconds is None:
            raise ValueError(
                "Aikanäytepisteellä on oltava seconds; ilman sitä kahta "
                "näytepistettä ei voi erottaa toisistaan."
            )
        if self.sample_kind == "first_contact" and self.seconds is not None:
            raise ValueError(
                "Ensikontaktin näytepisteellä ei ole nimellistä sekuntilukua: "
                "hetki on eri joka kierroksella. Käytä seconds_mediania."
            )
        return self


class UtilityUse(_Node):
    """Yksi utility-kuvio: tyyppi, heittoalue, räjähdysalue ja aikaikkuna.

    Tästä luetaan tavoiteanalyysin rivi *"T-spawnista CT-savu B sitelle"*:
    ``grenade_type = "smoke"``, ``throw_area = "TSpawn"``,
    ``detonate_area = "BombsiteB"``.

    ``n`` on kierrosten määrä, ``throws`` heittojen määrä. Ne eroavat, kun
    samalla kierroksella heitetään kaksi samanlaista kranaattia samaan
    paikkaan -- ja juuri siksi ``n``-arvoja ei saa laskea yhteen kranaattien
    määräksi. Kranaattien määrä kierroksella on :class:`UtilityCounts`.
    """

    grenade_type: str
    #: Heittäjän oma alue heittohetkellä. **Havainto**, ei arvio.
    throw_area: str | None
    #: Räjähdyksen alue. **Arvio**: kranaatilla ei ole aluenimeä, joten se on
    #: luettu demon pistepilven lähimmästä ruudusta -- siitä kohdasta kartalla,
    #: jossa pelaajat ovat lähinnä räjähdystä oikeasti seisoneet.
    detonate_area: str | None
    #: Mistä ``detonate_area`` on peräisin. ``null`` aina ja vain silloin, kun
    #: ``detonate_area`` on ``null``. Ilman tätä raportti esittäisi arvion
    #: havaintona.
    area_source: AreaSource | None
    #: Aikaikkunan nimi, esimerkiksi ``"0-5"`` tai ``"20+"``. Rajat ovat
    #: ``[thresholds].utility_seconds_buckets``.
    seconds_bucket: str
    n: int = Field(gt=0)
    throws: int = Field(gt=0)
    m: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> UtilityUse:
        if self.n > self.m:
            raise AggregateError(
                f"Utility-kuvio {self.grenade_type} {self.throw_area!r} -> "
                f"{self.detonate_area!r} esiintyy {self.n} kierroksella, "
                f"vaikka kierroksia on {self.m}."
            )
        if self.throws < self.n:
            raise AggregateError(
                f"Utility-kuviolla {self.grenade_type} on {self.throws} "
                f"heittoa mutta {self.n} kierrosta; heittoja ei voi olla "
                "vähemmän kuin kierroksia."
            )
        if (self.area_source is None) != (self.detonate_area is None):
            raise ValueError(
                f"detonate_area={self.detonate_area!r} ja "
                f"area_source={self.area_source!r} ovat ristiriidassa: "
                "kumpikin on joko annettu tai molemmat tyhjiä. Alue ilman "
                "lähdettä esittäisi arvion havaintona, ja lähde ilman aluetta "
                "väittäisi johdosta alueelle, jota ei ole."
            )
        return self


class GrenadeCount(_Node):
    """Yksi pylväs jakaumassa "montako heitettiin kierroksella"."""

    thrown: int = Field(ge=0)
    n: int = Field(gt=0)


class UtilityCounts(_Node):
    """Yhden kranaattityypin määräjakauma kierroksittain.

    Tästä luetaan tavoiteanalyysin rivit *"2 savua 2 valoo"* ja *"3 polttoo,
    2 HE, 1 savu"*: kysymys ei ole siitä missä kranaatti räjähti vaan siitä,
    montako niitä heitettiin. :class:`UtilityUse` ei vastaa siihen, koska sen
    ``n`` laskee kierroksia eikä kranaatteja.

    Jakauma sisältää arvon ``thrown = 0`` samasta syystä kuin
    :class:`AreaDistribution` sisältää arvon ``players = 0``: ilman sitä
    ``Σ n = m`` ei pitäisi, eikä "eivät heittäneet yhtään savua" olisi
    havainto vaan puuttuva rivi.
    """

    grenade_type: str
    m: int = Field(ge=0)
    counts: list[GrenadeCount]

    @model_validator(mode="after")
    def _check_sample(self) -> UtilityCounts:
        total = sum(c.n for c in self.counts)
        if total != self.m:
            raise AggregateError(
                f"Otanta ei täsmää kranaattityypillä {self.grenade_type!r}: "
                f"n-arvojen summa on {total}, mutta kierroksia on {self.m}. "
                "Jokaisen kierroksen on tuotettava havainto -- myös silloin, "
                "kun heittoja oli nolla."
            )
        seen = [c.thrown for c in self.counts]
        if len(seen) != len(set(seen)):
            raise ValueError(
                f"Kranaattityypin {self.grenade_type!r} jakaumassa on sama "
                "lukumäärä kahdesti."
            )
        return self


class ArmedCount(_Node):
    """Yksi pylväs jakaumassa "montako pelaajaa oli aseistettu"."""

    armed: int = Field(ge=0)
    n: int = Field(gt=0)


class ArmedPlayers(_Node):
    """Aseistettujen pelaajien määrä ostoajan lopussa, kierroksittain.

    Havainto on Story 1.6:n laskuri ``players_armed_buy_end``: pelaajalla oli
    panssari **ja** vähintään yksi ase hallussa. Se on hallussapito eikä
    ostos, joten säästetty kivääri laskeutuu samoin kuin ostettu.

    **Tästä EI lueta** tavoiteanalyysin rivejä *"5 kevlaria"* ja *"ei
    kevuja"*: ne ovat :class:`ArmoredPlayers`issä. Pistoolikierroksella tämä
    jakauma on käytännössä ``0``, koska 800 dollarin aloitusrahalla ei osta
    sekä kevlaria (650) että parannettua asetta -- aiempi versio tästä
    docstringistä väitti päinvastaista, ja se väärinluenta maksoi Story 2.3:n
    hyväksymisajossa yhden väärän rivin.

    ``m`` on niiden kierrosten määrä, joilta havainto **saatiin**;
    ``rounds_unknown`` on loput. Ne on pidettävä erillään: nolla aseistettua
    on eri asia kuin lukukelvoton tavaraluettelo, ja jälkimmäinen näyttäisi
    säästökierrokselta.
    """

    m: int = Field(ge=0)
    rounds_unknown: int = Field(ge=0)
    counts: list[ArmedCount]

    @model_validator(mode="after")
    def _check_sample(self) -> ArmedPlayers:
        total = sum(c.n for c in self.counts)
        if total != self.m:
            raise AggregateError(
                "Otanta ei täsmää aseistettujen pelaajien jakaumassa: "
                f"n-arvojen summa on {total}, mutta havaintoja on {self.m}."
            )
        return self


class ArmoredCount(_Node):
    """Yksi pylväs jakaumassa "montako pelaajaa kantoi panssaria".

    Kenttä on ``armored`` eikä ``armed`` tarkoituksella: ``report.json``
    luetaan myös käsin, ja kaksi lähes samannimistä jakaumaa sekoittuisi
    keskenään, jos ne käyttäisivät samaa kenttänimeä.
    """

    armored: int = Field(ge=0)
    n: int = Field(gt=0)


class ArmoredPlayers(_Node):
    """Panssaria kantaneiden pelaajien määrä ostoajan lopussa, kierroksittain.

    **Tästä** luetaan tavoiteanalyysin rivit *"5 kevlaria"* (Nuke, T-pistooli)
    ja *"ei kevuja"* (Ancient, CT). Havainto on ``players_armored_buy_end``:
    pelaajalla oli panssaria (``m_ArmorValue > 0``) ostoajan lopussa. Kypärää
    ei eroteta, eikä vaurioitunutta panssaria ehjästä.

    **Eri luku kuin** :class:`ArmedPlayers`, ei sen yleistys. Ne vastaavat eri
    kysymyksiin ja molempia tarvitaan:

    * aseistettu = panssari **ja** parannettu ase -- puolioston kalibroitu
      ehto A, jonka ``classify`` lukee
    * panssaroitu = panssari, piste -- "monellako oli panssari"

    Ne ovat **sisäkkäisiä eivätkä rinnakkaisia**: aseistetun ehto sisältää
    panssarin, joten aseistetut ovat panssaroitujen osajoukko. Molemmat
    luetaan samalta tickiltä ja samasta pelaajajoukosta, joten myös jakajat
    ovat samat.

    **Hallussapito, ei ostos.** Panssari säilyy kierroksen yli hengissä
    selvinneellä, joten muilla kierrostyypeillä luku kertoo mitä pelaajilla
    oli eikä mitä he ostivat. **Pistoolikierros (1 ja 13) on poikkeus**:
    puoliaika alkaa puhtaalta pöydältä eikä perintää ole, joten siellä luku on
    ostohavainto -- ja juuri siksi *"5 kevlaria"* on oikea luenta.

    Pistoolikierroksella laskurit myös eroavat eniten: mitattu neljästä
    MatureMayhem-demosta 2026-08-30, kaikilla kahdeksalla pistoolikierroksella
    aseistettuja 0 ja panssaroituja 1--5. Sääntö se ei ole vaan rahan seuraus,
    ja poimittu ase riittää aseistamaan: samassa aineistossa vastustajan
    Anubis-kierroksella 13 laskurit ovat 3 ja 1.

    ``m`` on niiden kierrosten määrä, joilta havainto **saatiin**;
    ``rounds_unknown`` on loput. Sama erottelu kuin
    :class:`ArmedPlayers`issä: nolla panssaroitua on havainto, lukukelvoton
    panssari ei ole havainto lainkaan.
    """

    m: int = Field(ge=0)
    rounds_unknown: int = Field(ge=0)
    counts: list[ArmoredCount]

    @model_validator(mode="after")
    def _check_sample(self) -> ArmoredPlayers:
        total = sum(c.n for c in self.counts)
        if total != self.m:
            raise AggregateError(
                "Otanta ei täsmää panssaroitujen pelaajien jakaumassa: "
                f"n-arvojen summa on {total}, mutta havaintoja on {self.m}."
            )
        return self


class FirstContactArea(_Node):
    """Alue, jolla joukkueella oli pelaaja ensikontaktin hetkellä.

    Tästä luetaan rivi *"Otti kontaktin partsi käytävällä"*. Havainto on
    **läsnäolo**, ei pelaajamäärä: ``n`` on kierrokset, joilla alueella oli
    ainakin yksi elossa oleva pelaaja sillä hetkellä, kun kierroksen
    ensimmäinen ristiinpuolinen osuma tapahtui.

    ``Σ n = m`` **ei päde tässä** eikä ole tarkoituskaan: sama kierros tuottaa
    havainnon jokaiselle alueelle, jolla joukkueella oli pelaaja. Täysi
    jakauma samalta hetkeltä on ``positions``-listan ``first_contact``
    -näytepisteessä.
    """

    area: str | None
    n: int = Field(gt=0)
    m: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> FirstContactArea:
        if self.n > self.m:
            raise AggregateError(
                f"Ensikontaktin alue {self.area!r} esiintyy {self.n} "
                f"kierroksella, vaikka kierroksia on {self.m}."
            )
        return self


class FirstDeathArea(_Node):
    """Alue, jolla joukkue menetti **ensimmäisen** pelaajansa kierroksella.

    Tästä luetaan tavoiteanalyysin rivi *"Luola kuolee nii pelaa
    siteltä/nyypästä ja longilta"*: kierroksen ensimmäinen oma kuolema on se,
    joka selittää mitä joukkue teki sen jälkeen.

    ``Σ n = m`` **pätee tässä**, toisin kuin :class:`FirstContactArea`ssa:
    jokaisella kierroksella on täsmälleen yksi ensimmäinen kuolema, joten se
    tuottaa havainnon täsmälleen yhdelle alueelle. ``m`` on niiden kierrosten
    määrä, joilla joukkue **menetti pelaajan**; kierrokset, joilla kukaan ei
    kuollut, ovat :attr:`DeathReport.rounds_missing`issä eivätkä nollarivinä
    -- nollarivi väittäisi havainnoksi sen, ettei havaintoa ole.
    """

    #: Uhrin oma ``last_place_name`` kuolinhetkellä. **Havainto**, ei arvio.
    #: ``null`` = pelin aluenimeä ei saatu; rivi ei katoa.
    area: str | None
    n: int = Field(gt=0)
    m: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> FirstDeathArea:
        if self.n > self.m:
            raise AggregateError(
                f"Ensimmäisen kuoleman alue {self.area!r} esiintyy {self.n} "
                f"kierroksella, vaikka kierroksia on {self.m}."
            )
        return self


class KillArea(_Node):
    """Alue, jolta joukkueen pelaaja teki tapon.

    Tästä luetaan tavoiteanalyysin rivi *"Vihu meni secret pihalta"*: alue on
    **ampujan oma** ``last_place_name`` tappohetkellä, ei uhrin.

    ``m`` **ei ole kierroksia vaan tappoja**, ja ``Σ n = m`` sen yli. Ero
    :class:`AreaDistribution`iin on olennainen: siellä jokainen kierros
    tuottaa yhden havainnon jokaiselle alueelle, tässä jokainen **tappo**
    tuottaa yhden havainnon yhdelle alueelle. Kierrostyypillä voi olla
    enemmän tappoja kuin kierroksia, joten lukua ei saa lukea muodossa
    "n kierroksella m:stä".
    """

    area: str | None
    n: int = Field(gt=0)
    m: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> KillArea:
        if self.n > self.m:
            raise AggregateError(
                f"Tappoalue {self.area!r} esiintyy {self.n} tapossa, vaikka "
                f"tappoja on {self.m}."
            )
        return self


class DeathReport(_Node):
    """Joukkueen omat kuolemat ja tapot yhdellä kierrostyypillä.

    Kaksi reunajakaumaa, ja niillä on **eri nimittäjä**. Sekaannus olisi
    helppo ja kallis, joten se on kirjoitettu tähän:

    ``first_death_areas``
        Missä joukkue menetti ensimmäisen pelaajansa. Yksi havainto per
        kierros, joten ``Σ n = m`` ja ``m`` on **kierroksia**.
    ``kills``
        Mistä joukkueen pelaajat tekivät tappoja. Yksi havainto per **tappo**,
        joten ``Σ n = kills_total`` ja luku voi ylittää kierrosten määrän.

    ``rounds_missing`` on ne kierrokset, joilla joukkue **ei menettänyt
    yhtään pelaajaa**. Se on oma lukunsa eikä nollarivi: alue "ei kuollut" ei
    ole alue, ja ilman erillistä lukua ``Σ n = m`` pettäisi.

    **Omat tapot sisältävät teamkillin.** Jos joukkueen pelaaja tappaa
    joukkuekaverinsa, rivi on sekä oma kuolema että oma tappo. Kummankaan
    pois suodattaminen olisi tulkintaa: havainto on, että pelaaja kuoli ja
    että ampuja oli tietyllä alueella. Teamkill on harvinainen (1 kpl 591
    kuolemasta, mitattu 2026-08-30), mutta jos se joskus näkyy raportin
    luvussa, se näkyy siksi että se tapahtui.

    **Itsemurha ei ole tappo.** Jos ampuja ja uhri ovat sama pelaaja, rivi on
    oma kuolema muttei oma tappo. Se ei ole tulkinta vaan sama havainto
    luettuna oikein: "tapot alueittain" kertoo, **mistä joukkue ampuu**, ja
    itsemurhan alue on paikka, josta kukaan ei ampunut. Mitattu aineistossa
    0/591, joten vika olisi ollut latentti -- ja siksi se on kirjoitettu
    säännöksi eikä jätetty tapahtumatta.
    """

    #: Kierrokset, joilla joukkue menetti vähintään yhden pelaajan.
    m: int = Field(ge=0)
    #: Kierrokset, joilla joukkue ei menettänyt yhtäkään pelaajaa.
    rounds_missing: int = Field(ge=0)
    #: Ensimmäisen oman kuoleman ajoituksen mediaani sekunteina, tai ``null``
    #: jos yhdeltäkään kierrokselta ei saatu ajoitusta.
    first_death_seconds_median: float | None = None
    first_death_areas: list[FirstDeathArea] = Field(default_factory=list)
    #: Joukkueen omat tapot yhteensä. Tämä on ``kills``-listan nimittäjä.
    kills_total: int = Field(default=0, ge=0)
    kills: list[KillArea] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_first_death_sample(self) -> DeathReport:
        """``Σ n = m`` ja jokainen alue jakaa saman otannan.

        Ilman jälkimmäistä kaksi aluetta voisi väittää eri nimittäjää, ja
        raportin kaksi lukua samasta jakaumasta eivät olisi vertailukelpoisia.
        """
        for entry in self.first_death_areas:
            if entry.m != self.m:
                raise AggregateError(
                    f"Ensimmäisen kuoleman alue {entry.area!r} väittää "
                    f"otannakseen {entry.m}, mutta kierroksia joilla joukkue "
                    f"menetti pelaajan on {self.m}."
                )
        total = sum(entry.n for entry in self.first_death_areas)
        if total != self.m:
            raise AggregateError(
                "Otanta ei täsmää ensimmäisen kuoleman alueissa: n-arvojen "
                f"summa on {total}, mutta kierroksia joilla joukkue menetti "
                f"pelaajan on {self.m}.\n"
                "Jokaisella sellaisella kierroksella on täsmälleen yksi "
                "ensimmäinen kuolema, joten summan on oltava sama luku."
            )
        seen = [entry.area for entry in self.first_death_areas]
        if len(seen) != len(set(seen)):
            raise ValueError(
                "Ensimmäisen kuoleman jakaumassa on sama alue kahdesti."
            )
        return self

    @model_validator(mode="after")
    def _check_kill_sample(self) -> DeathReport:
        """``Σ n = kills_total``, ja nimittäjä on tappoja eikä kierroksia."""
        for entry in self.kills:
            if entry.m != self.kills_total:
                raise AggregateError(
                    f"Tappoalue {entry.area!r} väittää otannakseen "
                    f"{entry.m}, mutta tappoja on {self.kills_total}."
                )
        total = sum(entry.n for entry in self.kills)
        if total != self.kills_total:
            raise AggregateError(
                "Otanta ei täsmää tappoalueissa: n-arvojen summa on "
                f"{total}, mutta tappoja on {self.kills_total}.\n"
                "Jokainen tappo kuuluu täsmälleen yhdelle alueelle -- myös "
                "silloin, kun alue on tuntematon."
            )
        seen = [entry.area for entry in self.kills]
        if len(seen) != len(set(seen)):
            raise ValueError("Tappojakaumassa on sama alue kahdesti.")
        return self

    @model_validator(mode="after")
    def _check_median_has_a_sample(self) -> DeathReport:
        """Mediaani ilman yhtäkään kuolemaa olisi luku tyhjästä."""
        if self.first_death_seconds_median is not None and self.m == 0:
            raise AggregateError(
                "Ensimmäisen kuoleman mediaani on "
                f"{self.first_death_seconds_median}, mutta yhdelläkään "
                "kierroksella ei kuollut ketään. Mediaani ilman havaintoja "
                "olisi luku tyhjästä."
            )
        return self


class RoundTypeReport(_Node):
    """Yhden kierrostyypin kaikki havainnot yhdellä kartalla ja puolella.

    ``small_sample`` on merkintä eikä suodatin: alle
    ``[thresholds].small_sample_rounds`` kierroksen otanta esitetään yhä, mutta
    merkittynä. Yksi toistuma ei ole kuvio, ja raportin on sanottava se.
    """

    round_type: RoundType
    sample: Sample
    small_sample: bool
    positions: list[Position]
    utility: list[UtilityUse]
    utility_counts: list[UtilityCounts]
    players_armed: ArmedPlayers
    #: Panssaroidut omana havaintonaan aseistettujen rinnalla. **Ei oletusta**
    #: samasta syystä kuin ``deaths``illä: tyhjä oletus antaisi vanhalla
    #: versiolla lasketun haaran näyttää kierrostyypiltä, jolla kenelläkään ei
    #: ollut panssaria -- ja juuri sen eron skeemaversio erottaa.
    players_armored: ArmoredPlayers
    first_contact: list[FirstContactArea]
    #: Omat kuolemat ja tapot. Ei oletusta: tyhjä oletus antaisi vanhalla
    #: versiolla lasketun haaran näyttää kierrostyypiltä, jolla kukaan ei
    #: kuollut -- ja juuri se on ero, jonka skeemaversio erottaa.
    deaths: DeathReport

    @model_validator(mode="after")
    def _check_deaths_cover_the_rounds(self) -> RoundTypeReport:
        """Kuolemien kierrokset ovat täsmälleen kierrostyypin kierrokset.

        ``Σ n = m`` valvotaan :class:`DeathReport`in sisällä, mutta se pitää
        myös silloin kun ``m`` on laskettu **väärästä kierrosjoukosta**:
        jakauma olisi sisäisesti johdonmukainen ja hiljaa väärä. Jokainen muu
        taso tarkistaa kierrossummansa ylöspäin (:func:`_check_rounds_add_up`),
        ja tämä on kuolemien vastine sille.

        Raises:
            AggregateError: Jos kuolleet ja kuolemattomat kierrokset eivät
                yhdessä ole kierrostyypin otanta.
        """
        covered = self.deaths.m + self.deaths.rounds_missing
        if covered != self.sample.rounds:
            raise AggregateError(
                f"Kierrostyypin {self.round_type} kuolemat kattavat "
                f"{covered} kierrosta ({self.deaths.m} joilla joukkue "
                f"menetti pelaajan, {self.deaths.rounds_missing} joilla ei), "
                f"mutta kierrostyypin otanta on {self.sample.rounds} "
                "kierrosta.\n"
                "Ero tarkoittaa, että kuolemat on laskettu eri "
                "kierrosjoukosta kuin muut havainnot -- jakauma näyttäisi "
                "silti sisäisesti oikealta."
            )
        return self


class SideReport(_Node):
    """Yhden puolen kierrostyypit yhdellä kartalla."""

    side: Side
    sample: Sample
    round_types: list[RoundTypeReport]

    @model_validator(mode="after")
    def _check_rounds(self) -> SideReport:
        _check_rounds_add_up(
            self.sample, [rt.sample for rt in self.round_types], "puoli", "kierrostyyppi"
        )
        return self


class MapReport(_Node):
    """Yhden kartan molemmat puolet.

    ``map_name`` on ensisijaisesti **havainto**: ``parse`` lukee kartan nimen
    demon otsikosta ``MATCH``-tauluun (Story 2.11), eikä sitä validoida
    karttapoolia vasten -- poolin ulkopuolinen kartta on aito havainto.
    Havainnon puuttuessa nimi päätellään ``map_demo_id``:stä karttapoolia
    vasten, ja tuntematonkaan kartta ei katoa: silloin nimi on ``map_demo_id``
    itse ja lähde ``unknown``.

    ``map_name_source`` kertoo mistä nimi tuli, ja sen arvot ovat
    ensisijaisuusjärjestyksessä: ``demo_header`` -> ``map_demo_id`` ->
    ``unknown``.
    """

    map_name: str
    map_name_source: MapNameSource
    #: Kartan demot. Kaksi demoa samalta kartalta summautuu yhdeksi haaraksi,
    #: ja tämä lista kertoo mistä.
    map_demo_ids: list[str]
    sample: Sample
    sides: list[SideReport]

    @model_validator(mode="after")
    def _check_rounds(self) -> MapReport:
        _check_rounds_add_up(
            self.sample, [s.sample for s in self.sides], "kartta", "puoli"
        )
        if self.sample.demos != len(self.map_demo_ids):
            raise AggregateError(
                f"Kartta {self.map_name} väittää otannakseen "
                f"{self.sample.demos} demoa, mutta listaa "
                f"{len(self.map_demo_ids)}: "
                f"{', '.join(self.map_demo_ids)}.\n"
                "Kartan demot on lueteltava tarkalleen, koska juuri niistä "
                "kierrokset summautuivat."
            )
        return self


class RosterEntry(_Node):
    """Yksi rosteririvi: pelaaja tunnisteineen ja nimineen.

    **Molemmat, aina.** SteamID64 säilyy nimen rinnalla, koska nimi on
    luettavuutta varten mutta tunniste on ainoa jäljitettävä arvo: nimi voi
    vaihtua ottelusta toiseen, tunniste ei.

    ``display_name`` on ``None``, jos nimeä ei saatu luettua demosta. Se ei ole
    sama asia kuin tyhjä merkkijono eikä sitä korvata tunnisteella tässä --
    korvaus on esitysvalinta ja kuuluu ``render``-vaiheeseen.
    """

    player_id: str
    display_name: str | None = None

    @field_validator("display_name")
    @classmethod
    def _empty_is_not_a_name(cls, value: str | None) -> str | None:
        """Tyhjä merkkijono ei ole nimi -- se on ``None``.

        Ilman tätä rosterirvi näyttäisi tyhjän nimen SteamID:n vieressä, mikä
        lukee kuin nimi olisi tyhjä eikä kuin sitä ei olisi. ``TeamReport``
        vartioi saman asian joukkueen nimelle; pelaajan nimi ei voi olla
        löysempi.
        """
        if value is None:
            return None
        return value.strip() or None


class TeamReport(_Node):
    """Joukkue, jonka näkökulmasta raportti on tehty.

    ``key`` on ``classified/``-hakemiston nimi. Ennen Epic 3:a se on
    kokoonpanotunniste (``lineup_key``); ``lineup_keys`` kertoo, mitkä
    kokoonpanot liitettiin samaksi joukkueeksi ja millä perusteella
    (``[thresholds].team_identity_min_common`` yhteistä pelaajaa).

    **Nimi on havainto, ei johdos.** ``display_name`` on joukkueen klaaninimi
    demosta (``LINEUPS.clan_name``) silloin ja vain silloin, kun
    ``display_name_source`` on ``clan_name``. Ilman havaintoa nimi on tunniste
    ja lähde ``team_key``, ja raportti sanoo sen ääneen sen sijaan että
    esittäisi tiivisteen nimenä.
    """

    key: str
    slug: str
    display_name: str
    #: Mistä ``display_name`` tulee. ``clan_name`` = havaittu demosta;
    #: ``team_key`` = havaintoa ei ole, joten nimi on tunniste itse.
    display_name_source: Literal["clan_name", "team_key"] = "team_key"
    #: Muut klaaninimet, joita liitetyistä demoista havaittiin. Ristiriita ei
    #: katoa: näytettäväksi valitaan useimmin havaittu, ja loput luetellaan
    #: tässä, jotta lukija näkee että joukkue esiintyi kahdella nimellä.
    display_name_alternatives: list[str] = Field(default_factory=list)
    lineup_keys: list[str]
    roster: list[RosterEntry]
    #: Mistä ``roster`` tulee. ``lineups`` = havaittu demoista;
    #: ``index`` = joukkueindeksistä (Epic 3, ei vielä olemassa).
    roster_source: Literal["lineups", "index"]

    @model_validator(mode="after")
    def _check_name_source(self) -> TeamReport:
        """Lähde, nimi, vaihtoehdot ja slug eivät saa olla eri mieltä.

        Ilman tätä ``display_name_source = "clan_name"`` yhdessä tunnisteen
        kanssa väittäisi tiivistettä havaituksi nimeksi -- ja raportin otsikko
        rakentuu juuri tämän eron varaan.

        **Slug on osa samaa väitettä.** Se päätyy tiedostonimeen, jonka lukija
        näkee ennen kuin avaa raportin; slug, joka on eri mieltä nimen kanssa,
        nimeäisi tiedoston joukkueen mukaan jota raportti ei käsittele. Sitä ei
        voi vartioida erikseen, koska juuri pari (nimi, slug) on se väite.

        **Vaihtoehtoiset nimet ovat havaintoja.** Tyhjä merkkijono ei ole nimi,
        sama nimi kahdesti ei ole kaksi havaintoa, eikä näytettävä nimi ole
        oma vaihtoehtonsa. ``aggregate`` estää nämä jo laskiessaan, mutta
        ``render`` ja levyltä luettu ``report.json`` nojaavat tähän sopimukseen
        eivätkä siihen laskentaan.
        """
        self._check_alternatives()
        self._check_slug()
        if self.display_name_source == "team_key":
            if self.display_name != self.key:
                raise AggregateError(
                    f"Joukkueen nimeksi on merkitty {self.display_name!r}, "
                    f"mutta lähteeksi {self.display_name_source!r}, joka "
                    f"tarkoittaa ettei nimeä ole -- silloin nimen on oltava "
                    f"tunniste {self.key!r} itse."
                )
            if self.display_name_alternatives:
                raise AggregateError(
                    "Joukkueelle on merkitty vaihtoehtoisia nimiä "
                    f"({', '.join(self.display_name_alternatives)}), mutta "
                    "lähteeksi 'team_key', joka tarkoittaa ettei yhtään nimeä "
                    "havaittu. Vaihtoehdot ovat havaintoja, joten niitä ei voi "
                    "olla ilman havaittua nimeä."
                )
        elif not self.display_name.strip():
            raise AggregateError(
                "Joukkueen nimeksi on merkitty tyhjä merkkijono, vaikka "
                "lähteeksi on merkitty 'clan_name'. Tyhjä merkkijono ei ole "
                "nimi -- silloin lähde on 'team_key'."
            )
        return self

    def _check_alternatives(self) -> None:
        """Vaihtoehtoiset nimet ovat havaintoja, eivät koristeita."""
        blank = [name for name in self.display_name_alternatives if not name.strip()]
        if blank:
            raise AggregateError(
                f"Joukkueen {self.key} vaihtoehtoisissa nimissä on "
                f"{len(blank)} tyhjää merkkijonoa. Tyhjä merkkijono ei ole "
                "nimi, eikä havaintoa voi esittää sellaisena."
            )
        seen = sorted(
            {
                name
                for name in self.display_name_alternatives
                if self.display_name_alternatives.count(name) > 1
            }
        )
        if seen:
            raise AggregateError(
                f"Joukkueen {self.key} vaihtoehtoiset nimet toistuvat: "
                f"{', '.join(seen)}. Sama nimi kahdesti ei ole kaksi "
                "havaintoa, ja luettelo väittäisi useampaa ristiriitaa kuin "
                "havaittiin."
            )
        if self.display_name in self.display_name_alternatives:
            raise AggregateError(
                f"Joukkueen näytettävä nimi {self.display_name!r} on myös "
                "omien vaihtoehtojensa joukossa. Vaihtoehdot ovat ne nimet, "
                "joita EI valittu -- muuten raportti luettelisi valitun nimen "
                "ristiriitana itsensä kanssa."
            )

    def _check_slug(self) -> None:
        """Slug johdetaan näytettävästä nimestä, varapolkuna tunniste.

        Sama sääntö kuin ``aggregate``ssa, kirjoitettuna tähän sopimukseen:
        levyltä luettu ``report.json`` ei ole käynyt sen laskennan läpi.
        """
        expected = slugify(self.display_name) or slugify(self.key) or SLUG_FALLBACK
        if self.slug != expected:
            raise AggregateError(
                f"Joukkueen slug on {self.slug!r}, mutta nimestä "
                f"{self.display_name!r} johdettuna se olisi {expected!r}.\n"
                "Slug päätyy raportin tiedostonimeen, joten eri mieltä oleva "
                "slug nimeäisi tiedoston joukkueen mukaan, jota raportti ei "
                "käsittele."
            )


class MissingDemo(_Node):
    """Ottelu, joka kuuluisi otantaan mutta jonka dataa ei ole.

    Puuttuva demo ei katoa hiljaa: se on tässä listassa syyn kanssa, ja
    raportti kertoo sen. Yksittäinen puuttuva demo ei kaada ajoa.
    """

    match: str
    reason: str


#: Mistä kartan nimi on peräisin, ensisijaisuusjärjestyksessä.
#:
#: Luettelo on täällä, koska sitä lukee nyt kaksi solmua
#: (:class:`MapReport` ja :class:`Anomaly`) eikä yksi. Kahtena kirjoitettuna
#: uusi lähde kelpaisi toisessa ja kaatuisi toisessa.
MAP_NAME_SOURCES: tuple[str, ...] = ("demo_header", "map_demo_id", "unknown")
MapNameSource = Literal["demo_header", "map_demo_id", "unknown"]


class AreaOrientation(_Node):
    """Alueen puoliorientaatio **yhdessä demossa**.

    Poikkeaman todistuskappale: alue on T:n aluetta siinä demossa, jos sen
    elossa-havainnoista aikanäytepisteillä vähintään ``advance_t_share``
    tulee T-puolelta. Luku on demon oma havainto -- ei karttatietokantaa, ei
    ihmisen antamaa aluejakoa, ei arkiston yli kertyvää taulua. Karttuva lähde
    antaisi samalle demolle eri tuloksen sen mukaan, mitä muita demoja
    arkistossa sattuu olemaan.

    **Rivi per demo eikä yksi luku.** Kaksi demoa samalta kartalta on yksi
    haara (Story 2.11), ja niiden T-osuudet voivat erota. Yksi luku
    pakottaisi valitsemaan keskiarvon (jota ei ole havaittu) tai ääriarvon
    (joka kertoisi vain toisesta demosta), joten poikkeama kantaa jokaisen
    demonsa orientaation erikseen.

    Attributes:
        map_demo_id: Demo, jonka havainto tämä on.
        t_share: T-havaintojen osuus alueen kaikista havainnoista.
        observations: Alueen kaikki elossa-havainnot, eli orientaation oma
            otanta. Ilman sitä osuus 1,00 näyttäisi samalta yhdestä ja
            sadasta havainnosta.
    """

    map_demo_id: str
    t_share: float = Field(ge=0.0, le=1.0)
    observations: int = Field(gt=0)


def _check_seconds(seconds: list[float], where: str) -> None:
    """Näytepisteet: äärellisiä, ei-negatiivisia ja toisistaan eroavia.

    Kolme ehtoa yhdessä paikassa, jotta jokainen näytepistelista on
    tarkistettu samoilla ehdoilla. NaN on tässä pahempi kuin väärä luku: se
    läpäisisi jokaisen vertailun epätotena, ja raportti muotoilisi sen
    muodossa ``nan s kohdalla`` -- eli lukuna, jota lukija ei voi tulkita.
    """
    for value in seconds:
        if not isfinite(value):
            raise ValueError(
                f"{where}: näytepiste {value!r} ei ole äärellinen luku. "
                "Näytepiste on sekuntimäärä kierroksen ankkurista."
            )
        if value < 0:
            raise ValueError(
                f"{where}: näytepiste {value:g} s on negatiivinen. "
                "Näytepisteet mitataan freezetimen lopusta eteenpäin, joten "
                "negatiivinen arvo osoittaisi ostoaikaan."
            )
    if len(set(seconds)) != len(seconds):
        raise ValueError(
            f"{where}: näytepisteet toistuvat ({seconds}); sama hetki on "
            "yksi havainto."
        )


class AnomalyRound(_Node):
    """Yksi kierros, jolla poikkeama havaittiin.

    **Tämä solmu on se, joka tekee rivistä luettavan oikein.** Crunchin
    lähtösuunnat ovat yhtäaikaisia vain saman kierroksen sisällä: kahden
    kierroksen suuntien yhdiste ("suunnista A, B, C ja D") lukisi neljäksi
    yhtäaikaiseksi suunnaksi, mikä on päinvastoin kuin määritelmä. Sama
    koskee näytepisteitä ja pelaajamäärää.

    Attributes:
        map_demo_id: Demo, jolta kierros on. Yhdellä kartalla voi olla kaksi
            demoa (Story 2.11), joten pelkkä kierrosnumero ei yksilöi.
        round_no: Kierrosnumero, jonka scoutti hakee demolta. Ilman sitä luku
            kertoisi että jotain tapahtui muttei missä sen näkee.
        round_type: Kierrostyyppi. Crunchilla se vaihtelee rivin sisällä,
            koska sääntö ei tunne kierrostyyppiä.
        seconds: Näytepisteet tällä kierroksella, nousevassa järjestyksessä.
        players_max: Suurin pelaajamäärä tämän kierroksen näytepisteillä.
        sources: Lähtöalueet tällä kierroksella -- **yhtäaikaiset**.
            Etenemisellä tyhjä.
    """

    map_demo_id: str = Field(min_length=1)
    round_no: int = Field(gt=0)
    round_type: RoundType
    seconds: list[float] = Field(min_length=1)
    players_max: int = Field(gt=0)
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_round(self) -> AnomalyRound:
        """Kierroksen sisäinen ristiriidattomuus.

        Raises:
            ValueError: Jos näytepisteet ovat mahdottomia, lähtöalueet
                toistuvat tai suuntia on enemmän kuin pelaajia. Viimeinen on
                mahdoton havainto: jokainen suunta tarvitsee oman pelaajansa,
                joten kolme suuntaa kahdella pelaajalla ei ole tiukempi
                havainto vaan rikkinäinen.
        """
        where = f"kierros {self.round_no} ({self.map_demo_id})"
        _check_seconds(self.seconds, where)
        if sorted(self.seconds) != self.seconds:
            raise ValueError(
                f"{where}: näytepisteet eivät ole nousevassa järjestyksessä "
                f"({self.seconds})."
            )
        if len(set(self.sources)) != len(self.sources):
            raise ValueError(
                f"{where}: lähtöalueet toistuvat ({self.sources}); sama "
                "suunta on yksi suunta."
            )
        # Nimettömyys ennen järjestystä: nimetön alue ei ole suunta lainkaan,
        # eikä sen paikasta luettelossa kannata kertoa mitään.
        if any(not name.strip() for name in self.sources):
            raise ValueError(
                f"{where}: lähtöalueiden joukossa on nimetön alue "
                f"({self.sources}). Nimetön alue ei ole suunta."
            )
        if sorted(self.sources) != self.sources:
            raise ValueError(
                f"{where}: lähtöalueet eivät ole aakkosjärjestyksessä "
                f"({self.sources}). Suunnat ovat yhtäaikaisia, joten niillä "
                "ei ole omaa järjestystä -- vakiojärjestys tekee raportin "
                "rivistä saman ajosta toiseen."
            )
        if len(self.sources) > self.players_max:
            raise ValueError(
                f"{where}: lähtöalueita on {len(self.sources)} mutta "
                f"pelaajia {self.players_max}. Jokainen suunta tarvitsee "
                "oman pelaajansa, joten havainto on rikkinäinen."
            )
        return self


class Anomaly(_Node):
    """Yksi poikkeava asetelma otantansa kanssa.

    Rivi on **yksi (sääntö, kartta, puoli, alue)** -yhdistelmä ja
    etenemisellä lisäksi kierrostyyppi, ei yksi kierros: sama alue kahdella
    eco-kierroksella on yksi rivi otannalla ``2/m``, ei kaksi riviä. Ilman
    ryhmittelyä toistuva poikkeama näyttäisi kahdelta eri havainnolta, ja
    juuri toistuminen on se, mikä erottaa suunnitelman sattumasta.

    **Nimittäjä on sääntökohtainen.** ``ct_advance`` on säästökierrosten
    ilmiö, joten ``m`` on sen kierrostyypin kierrokset kartalla ja puolella ja
    ``round_types`` on yksialkioinen. ``crunch`` ei tunne kierrostyyppiä, joten
    ``m`` on puolen **kaikki** kierrokset kartalla ja ``round_types`` kertoo
    millä tyypeillä se havaittiin. Yksi kierros on kelvollinen otanta; se
    merkitään pieneksi samalla säännöllä kuin muut (``small_sample``).

    Attributes:
        rule: ``ct_advance`` tai ``crunch``. Säännöt **jakavat**
            orientaatioehdon, mutta kumpikaan osumajoukko ei sisällä toista:
            crunch lisää suuntavaatimuksen ja pudottaa kierrostyyppirajauksen,
            joten säästökierroksella sama kierros tuottaa molemmat rivit ja
            täydellä ostolla vain crunchin.
        map_name: Kartta, jolla poikkeama havaittiin.
        map_name_source: Mistä kartan nimi tuli. Kannetaan siksi, että
            raportin runko puhuu nimillä (Story 2.12): kun lähde on
            ``unknown``, ``map_name`` **on** demotunniste, eikä sitä saa latoa
            runkoon paljaana.
        side: Subjektin puoli. Käytännössä aina ``CT``, koska molemmat säännöt
            tutkivat CT-rivejä; kenttä on rakenteessa, koska raportin rivi
            kertoo puolen eikä lukija saa päätellä sitä säännön nimestä.
        area: Pelin oma ``env_cs_place``-alue. **Ei koskaan tyhjä**: alue
            ilman nimeä ei voi olla T:n aluetta.
        round_types: Kierrostyypit, joilla poikkeama havaittiin,
            ``ROUND_TYPES``-järjestyksessä. Etenemisellä täsmälleen yksi.
        rounds: Kierrokset havaintoineen. Tästä luetaan "milloin", "mistä" ja
            "kuinka monta" niin, ettei rivi väitä yhtäaikaisuutta yli
            kierrosrajan.
        orientation: Alueen orientaatio niistä demoista, joissa poikkeama
            havaittiin -- poikkeaman todistuskappale.
        players_max: Suurin havaittu pelaajamäärä koko rivillä. Yhteenveto
            ``rounds``ista, ja malli valvoo että se vastaa niitä.
        n: Kierrokset, joilla poikkeama havaittiin (``len(rounds)``).
        m: Nimittäjä, ks. yllä.
        small_sample: Onko ``m`` alle ``small_sample_rounds``. Sama merkintä
            samalla säännöllä kuin muualla; ``render`` ei laske sitä.
    """

    rule: AnomalyRule
    map_name: str = Field(min_length=1)
    map_name_source: MapNameSource
    side: Side
    area: str = Field(min_length=1)
    round_types: list[RoundType] = Field(min_length=1)
    rounds: list[AnomalyRound] = Field(min_length=1)
    orientation: list[AreaOrientation] = Field(min_length=1)
    players_max: int = Field(gt=0)
    n: int = Field(gt=0)
    m: int = Field(gt=0)
    small_sample: bool = False

    @model_validator(mode="after")
    def _check_observation(self) -> Anomaly:
        """Yhteenveto ei voi olla eri mieltä kuin rivit joista se on koottu.

        Raises:
            AggregateError: Jos otanta on mahdoton (``n > m``) tai ``n`` ei
                ole kierroslistan pituus. Kumpikin tarkoittaa, että rivi ja
                sen todisteet ovat eri kokoisia.
            ValueError: Jos kierros esiintyy kahdesti, orientaatio ei kata
                juuri niitä demoja joilla poikkeama havaittiin, sääntö ja
                lähtöalueiden olemassaolo ovat ristiriidassa, tai
                ``round_types`` ei vastaa kierroksia.
        """
        if self.n != len(self.rounds):
            raise AggregateError(
                f"Poikkeama {self.rule} alueella {self.area!r} väittää "
                f"otannakseen {self.n} kierrosta, mutta kantaa "
                f"{len(self.rounds)} kierrosriviä. Luku ja sen todisteet "
                "ovat eri kokoisia."
            )
        if self.n > self.m:
            raise AggregateError(
                f"Poikkeama {self.rule} alueella {self.area!r} esiintyy "
                f"{self.n} kierroksella, vaikka kierroksia on {self.m}."
            )
        keys = [(entry.map_demo_id, entry.round_no) for entry in self.rounds]
        if len(set(keys)) != len(keys):
            raise ValueError(
                f"Poikkeaman {self.area!r} kierroslistassa on sama kierros "
                f"kahdesti ({sorted(keys)}); kierros on yksi havainto."
            )
        expected_types = [
            name for name in ROUND_TYPES
            if name in {entry.round_type for entry in self.rounds}
        ]
        if list(self.round_types) != expected_types:
            raise ValueError(
                f"Poikkeaman {self.area!r} round_types on {self.round_types}, "
                f"mutta kierrokset ovat tyypeiltään {expected_types}. "
                "Yhteenveto ei voi nimetä tyyppiä, jota yksikään kierros ei "
                "ole -- eikä jättää pois tyyppiä, joka on."
            )
        if self.rule == "ct_advance" and len(self.round_types) != 1:
            raise ValueError(
                f"CT-eteneminen alueella {self.area!r} kantaa "
                f"{len(self.round_types)} kierrostyyppiä ({self.round_types}). "
                "Eteneminen ryhmitellään kierrostyypin mukaan, koska se on "
                "säästökierrosten ilmiö ja kierrostyyppi on osa havaintoa, "
                "joten yhdellä rivillä on täsmälleen yksi tyyppi."
            )
        biggest = max(entry.players_max for entry in self.rounds)
        if self.players_max != biggest:
            raise ValueError(
                f"Poikkeaman {self.area!r} players_max on {self.players_max}, "
                f"mutta kierrosten suurin on {biggest}."
            )
        with_sources = [entry for entry in self.rounds if entry.sources]
        if self.rule == "crunch" and len(with_sources) != len(self.rounds):
            raise ValueError(
                f"Crunch alueella {self.area!r} kantaa kierroksia ilman "
                "lähtöalueita. Crunch on saapumista alueelle useasta "
                "suunnasta samaan aikaan, joten suunnaton kierros olisi eri "
                "sääntö samalla nimellä."
            )
        if self.rule == "ct_advance" and with_sources:
            raise ValueError(
                f"CT-eteneminen alueella {self.area!r} kantaa lähtöalueita, "
                "vaikka sääntö ei laske suuntia. Suunnat ovat crunchin "
                "havainto, ja etenemisrivillä ne väittäisivät mitatuksi "
                "jotain, jota ei mitattu."
            )
        demos = [entry.map_demo_id for entry in self.orientation]
        if len(set(demos)) != len(demos):
            raise ValueError(
                f"Poikkeaman {self.area!r} orientaatiossa on sama demo "
                f"kahdesti ({sorted(demos)}); alueella on demoa kohden yksi "
                "T-osuus."
            )
        seen = {entry.map_demo_id for entry in self.rounds}
        if set(demos) != seen:
            raise ValueError(
                f"Poikkeaman {self.area!r} orientaatio kattaa demot "
                f"{sorted(demos)}, mutta havainnot ovat demoista "
                f"{sorted(seen)}. Orientaatio on poikkeaman todistuskappale, "
                "joten sen on katettava täsmälleen ne demot joilla poikkeama "
                "havaittiin -- ei enempää eikä vähempää."
            )
        return self


class AnomalyScan(_Node):
    """Mitä poikkeamasäännöt saivat luettavakseen.

    **Tyhjä poikkeamaluku on havainto vain siitä, mitä tutkittiin.** Ilman
    tätä solmua "ei poikkeamia" lukisi mitattuna negatiivisena myös silloin,
    kun sääntöjä ei ajettu millekään kierrokselle tai kun jonkin demon
    orientaatio jäi tyhjäksi -- ja juuri se ero ("havainto eikä puute") on
    koko luvun arvo.

    Attributes:
        rules: Säännöt, jotka ajettiin.
        rules_deferred: Arkkitehtuurin (AD-10) nimeämät säännöt, joita ei ole
            toteutettu. Kattavuuden nimittäjä: lukija näkee montako
            selkärangan sääntöä jäi ajamatta.
        rounds_scanned: Kierrokset, jotka säännöt näkivät -- eli ne, joilla on
            kierrostyyppi. Luokittelemattomat eivät mahdu rakenteeseen
            lainkaan, ja niiden määrä on ``Report.unclassified_rounds``.
        crunch_rounds: Niistä ne, joilla **crunch voi osua**: subjektin
            CT-puolen kierrokset. Molemmat säännöt tutkivat vain CT-rivejä,
            joten T-puolen kierros ei voi tuottaa osumaa kummallakaan --
            ja ``rounds_scanned`` yksin lupaisi kattavuutta, jota ei ole.
        advance_rounds: Niistä ne, joilla **CT-eteneminen voi osua**:
            CT-puolen säästökierrokset. Kapein luku kolmesta, ja juuri se on
            etenemisen todellinen nimittäjä kattavuutena.
        demos_without_orientation: Demot, joiden näytepisteistä ei saatu
            yhtään aluetta havaintokynnyksen yli. Niillä molemmat säännöt
            vaikenevat, eikä se ole mitattu negatiivinen vaan sokea piste.
    """

    rules: list[AnomalyRule] = Field(min_length=1)
    rules_deferred: list[str] = Field(default_factory=list)
    rounds_scanned: int = Field(ge=0)
    crunch_rounds: int = Field(ge=0)
    advance_rounds: int = Field(ge=0)
    demos_without_orientation: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_scan(self) -> AnomalyScan:
        """Kolme lukua ovat sisäkkäisiä, ja järjestys on määritelmä.

        ``advance_rounds`` (CT + säästö) on osajoukko ``crunch_rounds``ista
        (CT), joka on osajoukko ``rounds_scanned``ista (kaikki). Väärä
        järjestys tarkoittaisi, että kattavuus lupaa säännölle enemmän
        kierroksia kuin sääntö voi tutkia.
        """
        if not self.advance_rounds <= self.crunch_rounds <= self.rounds_scanned:
            raise AggregateError(
                f"Kattavuusluvut eivät ole sisäkkäisiä: eteneminen "
                f"{self.advance_rounds}, crunch {self.crunch_rounds}, "
                f"kaikki {self.rounds_scanned}.\n"
                "CT-puolen säästökierrokset ovat osajoukko CT-kierroksista, "
                "jotka ovat osajoukko kaikista kierroksista."
            )
        unknown = sorted(set(self.rules) - set(ANOMALY_RULES))
        if unknown:
            raise ValueError(
                f"Tuntemattomia poikkeamasääntöjä: {unknown}. Sallitut ovat "
                f"{list(ANOMALY_RULES)}."
            )
        if len(set(self.rules)) != len(self.rules):
            raise ValueError(f"Sama sääntö kahdesti: {self.rules}.")
        if len(set(self.demos_without_orientation)) != len(
            self.demos_without_orientation
        ):
            raise ValueError(
                "Sama demo kahdesti orientaatiottomien listassa: "
                f"{self.demos_without_orientation}."
            )
        return self


class Report(_Node):
    """``aggregates/<team_key>/report.json``.

    Kaikki luvut on laskettu valmiiksi; ``render`` vain valitsee ja muotoilee.
    """

    schema_version: str = REPORT_SCHEMA_VERSION
    generated_at: datetime
    #: Työkalut, joiden versio vaikutti tähän tulokseen. Ei manifestin kenttä
    #: vaan jäljitettävyyttä varten: ``report.json`` ylikirjoitetaan aina, joten
    #: sen sisältö saa kertoa millä versiolla se tehtiin.
    tool_versions: dict[str, str] = Field(default_factory=dict)
    team: TeamReport
    sample: Sample
    #: ``[thresholds]``- ja ``[aggregate]``-osiot sellaisina kuin ne olivat
    #: **tätä aggregointia ajettaessa**. Ne eivät ole samat kuin ne, joilla
    #: kierrokset luokiteltiin -- luokittelu on eri vaihe ja voi olla ajettu
    #: eri asetuksilla. Luokittelun omat kynnykset ovat kentässä
    #: :attr:`classify_thresholds`, ja ne luetaan luokitellusta taulusta eikä
    #: nykyisistä asetuksista.
    thresholds_used: dict[str, Any] = Field(default_factory=dict)
    #: Ne kynnysarvot, joilla kierrokset **oikeasti luokiteltiin**, luettuna
    #: ``CLASSIFIED.inputs``-sarakkeesta. ``classify`` tallentaa jokaiselle
    #: kierrokselle vertailuun käytetyt arvot, joten tämä on havainto eikä
    #: nykyisten asetusten kopio. ``aggregate`` kieltäytyy, jos arvot eroavat
    #: kierrosten välillä: silloin raportti sekoittaisi eri säännöillä
    #: luokiteltuja kierroksia samaan lukuun.
    classify_thresholds: dict[str, int] = Field(default_factory=dict)
    #: Räjähdykset, joilta puuttuu heittorivi. Pari yhdistetään avaimella
    #: ``(map_demo_id, grenade_no)``, ja ``parse`` kirjoittaa ne aina parina --
    #: pariton rivi on siis merkki rikkoutuneesta taulusta. Se pudotetaan
    #: utilityn laskennasta, mutta lukumäärä on tässä, koska hiljainen pudotus
    #: näyttäisi siltä, ettei kranaattia heitetty.
    unpaired_detonations: int = Field(default=0, ge=0)
    missing_demos: list[MissingDemo] = Field(default_factory=list)
    #: Kierrokset, joilta kierrostyyppi puuttuu. Ne eivät ole rakenteessa --
    #: kierrostyyppitasoa ei voi rakentaa ilman tyyppiä -- mutta lukumäärä
    #: raportoidaan, jottei kierros katoa hiljaa.
    unclassified_rounds: int = Field(default=0, ge=0)
    #: Poikkeavat asetelmat, kaikki kartat ja puolet samassa listassa. Tyhjä
    #: lista on **havainto** eikä puute: "ei poikkeamia" on tulos, ja
    #: raportti sanoo sen ääneen omassa luvussaan -- mutta vain siitä, mitä
    #: :attr:`anomaly_scan` kertoo tutkitun.
    #:
    #: Lista on raportin juuressa eikä kierrostyypin alla, koska poikkeama on
    #: epicin arvokkain tuotos: 24 lohkoon hajotettuna se olisi juuri se
    #: ongelma, jonka Story 2.5 ratkaisee. Jokainen rivi kantaa siksi itse
    #: kartan, puolen ja kierrostyypit.
    anomalies: list[Anomaly] = Field(default_factory=list)
    #: Poikkeamasääntöjen kattavuus: mitä ajettiin, mille ja mikä jäi sokeaan
    #: pisteeseen. **Pakollinen eikä oletuksellinen**, koska juuri tyhjä
    #: poikkeamalista tarvitsee sitä: ilman kattavuutta "ei poikkeamia" ei
    #: erotu siitä, ettei sääntöjä ajettu.
    anomaly_scan: AnomalyScan
    maps: list[MapReport] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_rounds(self) -> Report:
        """Ylätason otanta on karttojen summa.

        ``unclassified_rounds`` **ei** ole mukana summassa eikä saa olla:
        kierros ilman kierrostyyppiä ei mahdu rakenteeseen lainkaan, joten se
        ei ole yhdenkään kartan, puolen eikä kierrostyypin otannassa. Se on
        oma lukunsa juuri siksi, ettei sitä laskettaisi mukaan väitteisiin,
        joita se ei tue.
        """
        _check_rounds_add_up(
            self.sample, [m.sample for m in self.maps], "raportti", "kartta"
        )
        demos = sum(m.sample.demos for m in self.maps)
        if self.sample.demos != demos:
            raise AggregateError(
                f"Raportin otanta väittää {self.sample.demos} demoa, mutta "
                f"karttojen summa on {demos}. Jokainen demo on täsmälleen "
                "yhdellä kartalla, joten summan on täsmättävä."
            )
        self._check_anomalies()
        return self

    def _check_anomalies(self) -> None:
        """Poikkeamat ovat puun ulkopuolella, joten side kiinnitetään täällä.

        Kaksi ehtoa, joita yksikään :class:`Anomaly` ei voi tarkistaa itse:
        rivi ei saa nimetä karttaa, jota raportissa ei ole (lukija etsisi
        karttalukua, jota ei kirjoitettu), eikä kaksi riviä saa jakaa samaa
        ryhmittelyavainta (silloin sama havainto olisi luvussa kahdesti eri
        luvuilla -- juuri se, minkä ryhmittely on olemassa estämään).

        Raises:
            AggregateError: Jos kartta puuttuu raportista tai avain toistuu.
        """
        known = {entry.map_name for entry in self.maps}
        missing = sorted(
            {a.map_name for a in self.anomalies if a.map_name not in known}
        )
        if missing:
            raise AggregateError(
                f"Poikkeama nimeää kartan, jota raportissa ei ole: {missing}. "
                f"Raportin kartat ovat {sorted(known)}.\n"
                "Lukija etsisi karttalukua, jota ei kirjoitettu."
            )
        keys = [
            (a.rule, a.map_name, a.side, a.area)
            + (tuple(a.round_types) if a.rule == "ct_advance" else ())
            for a in self.anomalies
        ]
        twice = sorted({key for key in keys if keys.count(key) > 1})
        if twice:
            raise AggregateError(
                f"Sama poikkeama on luvussa kahdesti: {twice}.\n"
                "Ryhmittelyavain on (sääntö, kartta, puoli, alue) ja "
                "etenemisellä lisäksi kierrostyyppi. Kaksi riviä samalla "
                "avaimella tarkoittaa, että ryhmittely ei tehnyt työtään: "
                "sama havainto näkyisi kahdesti eri otannoilla."
            )
