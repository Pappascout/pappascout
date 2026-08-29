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

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    "FirstContactArea",
    "RoundTypeReport",
    "SideReport",
    "MapReport",
    "TeamReport",
    "MissingDemo",
    "Report",
]

#: Raporttimallin skeemaversio. Nostetaan, kun rakenne muuttuu niin, ettei
#: vanha ``report.json`` enää validoidu -- silloin ``render`` kertoo, että
#: aggregointi on ajettava uudelleen, sen sijaan että se muotoilisi puolikkaan
#: raportin hiljaa.
REPORT_SCHEMA_VERSION = "1.0.0"


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

    Tästä luetaan tavoiteanalyysin rivit *"5 kevlaria"* ja *"ei kevuja"*.
    Havainto on Story 1.6:n laskuri ``players_armed_buy_end``: pelaajalla oli
    panssari **ja** vähintään yksi ase hallussa. Se on hallussapito eikä
    ostos, joten säästetty kivääri laskeutuu samoin kuin ostettu.

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
    first_contact: list[FirstContactArea]


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


class TeamReport(_Node):
    """Joukkue, jonka näkökulmasta raportti on tehty.

    ``key`` on ``classified/``-hakemiston nimi. Ennen Epic 3:a se on
    kokoonpanotunniste (``lineup_key``); ``lineup_keys`` kertoo, mitkä
    kokoonpanot liitettiin samaksi joukkueeksi ja millä perusteella
    (``[thresholds].team_identity_min_common`` yhteistä pelaajaa).
    """

    key: str
    slug: str
    display_name: str
    lineup_keys: list[str]
    roster: list[str]
    #: Mistä ``roster`` tulee. ``lineups`` = havaittu demoista;
    #: ``index`` = joukkueindeksistä (Epic 3, ei vielä olemassa).
    roster_source: Literal["lineups", "index"]


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
