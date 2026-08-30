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

Mitä täällä **ei** ole: tulkintoja. Sanoja "fake" tai "rush" ei esiinny
missään kentässä -- vain havaintoja ja lukumääriä. Poikkeavat asetelmat
(``anomalies``) ovat Story 2.5:n lisäys tähän samaan malliin.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pappascout.constants import (
    SAMPLE_BUCKETS,
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
    "Report",
]

#: Raporttimallin skeemaversio. Nostetaan, kun rakenne muuttuu niin, ettei
#: vanha ``report.json`` enää validoidu -- silloin ``render`` kertoo, että
#: aggregointi on ajettava uudelleen, sen sijaan että se muotoilisi puolikkaan
#: raportin hiljaa.
REPORT_SCHEMA_VERSION = "4.0.0"


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
    #: johdettu lähimmästä elossa olevasta pelaajasta.
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
                "väittäisi napsautusta alueelle, jota ei ole."
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

    ``map_name`` on **johdettu**, ei havaittu: kierros-, näytepiste- eikä
    tapahtumataulussa ole kartan nimeä, joten se päätellään ``map_demo_id``:stä
    karttapoolia vasten. ``map_name_source`` kertoo kummasta on kyse, eikä
    tuntematon kartta katoa: silloin nimi on ``map_demo_id`` itse ja lähde
    ``unknown``.
    """

    map_name: str
    map_name_source: Literal["map_demo_id", "unknown"]
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
        return self
