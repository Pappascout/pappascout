"""Typatut asetusmallit (AD-3) ja niiden lataus.

Asetukset on jaettu kahdeksaan osioon, joista jokainen on oma mallinsa. Osiointi on
**rakenteellinen**: vaihe ottaa parametrikseen vain oman osansa
(``parse(ParseSettings, ...)``, ``classify(ThresholdSettings, LeagueSettings, ...)``),
joten se ei pysty lukemaan muita osioita. Tämä on se mekanismi, joka tekee
lupauksesta "kynnysmuutos ei uudelleenparsi" rakenteellisen: ``parse``-manifestin
parametrihash lasketaan vain ``[parse]``-osiosta, eikä vaihe voi vahingossa
riippua ``[thresholds]``-arvoista, koska se ei näe niitä.

Avaimet eivät ole ``settings.toml``-tiedostossa. Ne luetaan
``%USERPROFILE%\\.pappascout\\.env``-tiedostosta ``SecretStr``-tyyppisinä;
projektin oma ``.env`` luetaan vain varalta. Syy on OneDrive: se tekisi
projektin ``.env``-tiedostosta konfliktikopioita kahdella koneella ja säilyttäisi
kierrätetyn avaimen versiohistoriassa.

Kaikki dollarimääräiset kynnysarvot ovat **per pelaaja**, ellei nimessä lue
muuta. Lähtöarvojen lähteet on merkitty ``settings.toml``-tiedostoon riveittäin.
"""

from __future__ import annotations

import os
import tomllib
from math import isfinite
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as _ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from pappascout.constants import seconds_label
from pappascout.errors import SettingsError

__all__ = [
    "ProjectSettings",
    "LeagueSettings",
    "ParseSettings",
    "ThresholdSettings",
    "AggregateSettings",
    "ReportSettings",
    "EconomySettings",
    "FaceitSettings",
    "Settings",
    "load_settings",
    "secrets_env_path",
    "project_env_path",
    "settings_search_paths",
    "find_settings_file",
    "SETTINGS_FILENAME",
    "SETTINGS_ENV_VAR",
    "SETTINGS_SECTIONS",
    "MAX_SNAPSHOT_SECONDS",
    "MAX_BUY_WINDOW_SECONDS",
    "MAX_ADVANCE_SAMPLE_SECONDS",
    "MAX_STACK_GROUP_MARGIN",
    "MAX_STACK_SITE_SEPARATION",
    "MAX_FACEIT_PAGE_SIZE",
    "MAX_FACEIT_RETRY_ATTEMPTS",
    "PLAYERS_ON_SERVER",
    "REMOVED_SETTINGS",
]

SETTINGS_FILENAME = "settings.toml"
SETTINGS_ENV_VAR = "PAPPASCOUT_SETTINGS"

#: Ainoat sallitut ylätason avaimet ``settings.toml``-tiedostossa (AD-3).
SETTINGS_SECTIONS: frozenset[str] = frozenset(
    {
        "project",
        "league",
        "parse",
        "thresholds",
        "aggregate",
        "report",
        "economy",
        "faceit",
    }
)

#: Asetukset, jotka on **poistettu**, ja se mihin ne menivät.
#:
#: ``extra="forbid"`` hylkää vanhan ``settings.toml``in joka tapauksessa, mutta
#: geneerisellä "tuntematon avain" -viestillä: käyttäjä näkisi vain, että hänen
#: tiedostonsa ei kelpaa, eikä sitä mitä tilalle tuli. Tämä taulu antaa
#: siirtymäohjeen -- kahden koneen arkistossa vanha tiedosto on tavallinen
#: tilanne, ei poikkeus.
REMOVED_SETTINGS: Final[dict[tuple[str, str], str]] = {
    ("parse", "armed_player_equip_min"): (
        "Poistui Story 1.6:ssa. Kalustolaskuri players_armed_buy_end ei "
        "enää vertaa varustearvoa kynnykseen vaan lukee havainnon: pelaaja on "
        "aseistettu, jos hänellä on panssari ja vähintään yksi ase hallussa. "
        "Aseluettelo on koodissa (src/pappascout/constants.py), koska se on "
        "pelin aseiden joukko eikä säädettävä arvo. Poista rivi tiedostosta -- "
        "mitään ei tarvitse laittaa tilalle."
    ),
    ("thresholds", "force_money_left_max"): (
        "Poistui Story 1.10:ssä. Se oli kiinteä raja taskuun jääneelle rahalle "
        "per pelaaja, eli joukkuesumma viidellä jaettuna -- ja juuri keskiarvo "
        "peitti sen, mistä puoliostossa on kyse. Tilalle tulivat "
        "normal_buy_money_min (oletus 4000), normal_buy_players_min (oletus 3) "
        "ja armed_players_min (oletus 3): puoliosto erotetaan forcesta sillä, "
        "moniko pelaaja pystyy normaaliin ostoon seuraavalla kierroksella, ja "
        "ecosta sillä, moniko oli aseistettu. Lisää kolme uutta riviä ja poista "
        "tämä."
    ),
}

#: Näytepisteen yläraja sekunteina. CS2:n kierros kestää 1.55 = 115 s, joten
#: sitä suurempi arvo ei voi osua yhdelläkään kierrokselle.
MAX_SNAPSHOT_SECONDS = 115.0

#: Pelaajia kentällä per joukkue. Pelin sääntö, ei asetus.
#:
#: Eri asia kuin ``[thresholds].roster_size``: rosterissa voi olla
#: vaihtopelaajia (mitattu: seitsemän pelaajaa yhdellä joukkueella), mutta
#: yhdelläkään kierroksella ei ole kuutta pelaajaa kentällä. Pelaajamäärää
#: koskevat kynnykset verrataan tähän, koska rosterin koko päästäisi läpi
#: ehdon, joka ei voi täyttyä.
PLAYERS_ON_SERVER = 5

#: Ostoikkunan yläraja sekunteina. **Oma raja, ei lainattu**
#: :data:`MAX_SNAPSHOT_SECONDS`ista: ne ovat kaksi riippumatonta asetusta, ja
#: yhteinen vakio kytkisi ne toisiinsa niin, että toisen säätäminen siirtäisi
#: toisen rajaa huomaamatta.
#:
#: Arvo on **kaksinkertainen pelin ostoaikaan** (20 s). Se päästää läpi
#: turnaussäännön, joka poikkeaa oletuksesta, mutta torjuu kirjoitusvirheen
#: (``200.0``) ja arvon, jolla mittauspiste ei enää olisi ostoaika vaan
#: mielivaltainen hetki kierroksen keskellä. Ilman omaa rajaa 100,0 s menisi
#: läpi äänettömästi.
MAX_BUY_WINDOW_SECONDS = 40.0

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]

#: Ei-negatiivinen liukuluku, jonka **ääretön ja NaN eivät kelpaa**.
#:
#: ``allow_inf_nan=False`` ei ole koristetta. Pistepilven painotus kertoo
#: arvolla ja vertaa tulosta kynnykseen; NaN tekisi jokaisesta vertailusta
#: epätoden, jolloin yksikään räjähdys ei saisi aluetta eikä mikään kertoisi
#: miksi. Ääretön tekisi saman toisin päin. Kumpikin läpäisisi pelkän
#: ``ge=0``-rajan (``nan >= 0`` on epätosi, mutta virheilmoitus puhuisi
#: väärästä asiasta, ja ``inf >= 0`` on tosi).
NonNegativeFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]

#: **Enemmistöosuus**: yli puolet, enintään kaikki. Ääretön ja NaN eivät kelpaa.
#:
#: Alaraja on ``> 0.5`` eikä ``>= 0``, ja se on määritelmä eikä makuasia.
#: Kynnys kertoo, kummalle puolelle alue **kuuluu**; arvolla 0,5 tai alle alue
#: voisi olla molempien, ja arvolla 0,0 jokainen kartan alue olisi T:n aluetta
#: -- eli sääntö laukeaisi joka kierroksella eikä poikkeamaluku enää mittaisi
#: poikkeamaa. Yläraja 1,0 on osuuden määritelmä: T-osuus 1,2 ei ole tiukempi
#: kynnys vaan mahdoton ehto, joka hiljentäisi säännön kokonaan.
#:
#: ``allow_inf_nan=False`` samasta syystä kuin :data:`NonNegativeFloat`illa:
#: NaN tekisi jokaisesta vertailusta epätoden, jolloin yksikään alue ei olisi
#: kummankaan puolen aluetta eikä mikään kertoisi miksi.
MajorityShare = Annotated[float, Field(gt=0.5, le=1.0, allow_inf_nan=False)]

#: Poikkeaman aikarajan yläraja sekunteina.
#:
#: Sama peruste kuin :data:`MAX_SNAPSHOT_SECONDS`illa mutta tiukempi luku:
#: raja **valikoi näytepisteistä**, joten sitä suurempi arvo ei rajaa mitään.
#: 45 s on suurin ``[parse].snapshot_seconds``-piste, ja mittaus osoitti sen
#: olevan juuri se piste, joka tuo epävarmuutta (kaksi CT:tä T:n spawnin
#: puolella voitetun kierroksen jälkeen). Yläraja 115,0 päästäisi läpi arvon,
#: joka näyttää rajalta muttei rajaa mitään; 60,0 päästää läpi jokaisen
#: mielekkään näytepistevalinnan ja torjuu kirjoitusvirheen ``300``.
MAX_ADVANCE_SAMPLE_SECONDS = 60.0

#: Stack-säännön ryhmämarginaalin yläraja.
#:
#: Marginaali on **suhdeluku**: alue kuuluu lähempään siteen vasta, kun toinen
#: site on vähintään tämän verran kauempana.
#:
#: **Siteet itse eivät katoa millään marginaalilla**, ja se on syytä sanoa
#: ääneen, koska päinvastainen on luonteva arvaus. Siten etäisyys omaan
#: keskipisteeseensä on aina 0, joten ehto "toinen on vähintään margin kertaa
#: kauempana" on sillä aina tosi -- ``BombsiteA`` on aina A:ssa ja
#: ``BombsiteB`` aina B:ssä. Yläraja torjuu siis sen, että **kaikki muut**
#: alueet putoavat ryhmättömiksi: sääntö vaatii vähintään neljä pelaajaa saman
#: siten ryhmässä, ja kahden alueen ryhmillä se voi täyttyä vain jos koko
#: puolustus seisoo sitellä itsellään. Sääntö näyttäisi silloin ajetulta
#: muttei mittaisi enää asetelmaa.
#:
#: Toinen puoli on tavallisempi: kirjoitusvirhe. Mitatut kartat erottuvat
#: marginaalilla 1,25, joten 10,0 päästää läpi jokaisen mielekkään valinnan ja
#: pysäyttää arvon, joka on selvästi väärää kokoluokkaa.
MAX_STACK_GROUP_MARGIN = 10.0

#: Stack-säännön erottuvuusvartijan yläraja.
#:
#: Sama peruste kuin marginaalilla ja poikkeaman aikarajalla: kynnyksellä on
#: oltava katto, koska sen ylittäminen ei tiukenna sääntöä vaan **hiljentää
#: sen kokonaan**. Suhde on mitattuna 0,47-5,04 (Nuke - Anubis), joten arvo
#: yli 20 vaientaisi jokaisen tunnetun kartan, ja raportti väittäisi "ei
#: stackeja" havaintona vaikka yhtäkään demoa ei tutkittu. 20,0 jättää
#: nelinkertaisen varan mitattuun maksimiin ja torjuu kirjoitusvirheen.
MAX_STACK_SITE_SEPARATION = 20.0

#: Pistepilven ruudun särmä. Rajat ovat suorituskykyä ja mielekkyyttä, eivät
#: makuasia. **Alaraja 8**: lähimmän haku on ristiintulo pisteiden ja ruutujen
#: välillä, ja särmä 1 tuottaisi luokkaa miljoona ruutua yhdestä demosta --
#: sadan tuhannen ruudun sijaan. **Yläraja 1024**: 32 pelaajan leveyttä, eli
#: sitä karkeampi ruudukko ei enää erottele kartan alueita toisistaan.
CalloutGridUnits = Annotated[int, Field(ge=8, le=1024)]

#: FACEIT Data API:n sivun yläraja. **Rajapinnan oma raja, ei makuasia**:
#: ``limit`` yli 100 ei tuo enempää rivejä vaan 400 Bad Requestin, ja
#: asetustiedostosta luettuna se kaataisi jokaisen haun tavalla, joka näyttäisi
#: verkkovialta.
#:
#: **Raja on täällä, mutta adapteri valvoo samaa vakiota.** Aiempi perustelu
#: väitti, ettei adapteri voi tarkistaa arvoa "koska se ei lue asetuksia" --
#: se ei ollut totta: :mod:`pappascout.adapters.faceit` tuo tämän vakion ja
#: hylkää rajan ulkopuolisen arvon itsekin. Adapteri ei *lue* asetustiedostoa,
#: mutta se ei myöskään hyväksy hiljaa sitä, minkä asetusmalli hylkää -- ja
#: hiljainen clamppaus tuottaisi juuri sen 400-virheen, joka näyttäisi
#: verkkovialta. Luku on silti vain täällä, jottei sitä ole kahdessa paikassa.
MAX_FACEIT_PAGE_SIZE = 100

#: Uudelleenyritysten yläraja. Kynnyksellä on oltava katto samasta syystä kuin
#: :data:`MAX_STACK_SITE_SEPARATION`illa, mutta toisin päin: liian **suuri**
#: arvo ei tiukenna mitään vaan muuttaa käyttäjälle näkyvän virheen tunnin
#: mittaiseksi hiljaisuudeksi. Kasvava viive tarkoittaa, että kymmenes yritys
#: on jo minuuttien päässä; kirjoitusvirhe ``100`` jumittaisi ajon ilman että
#: mikään kertoisi miksi.
MAX_FACEIT_RETRY_ATTEMPTS = 10


class _Section(BaseModel):
    """Asetusosion kantaluokka: tuntematon avain on virhe, ei hiljainen ohitus."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectSettings(_Section):
    """``[project]`` -- oma joukkue, arkisto ja ajon perusasetukset."""

    own_team_name: str
    archive_root: Path
    language: Literal["fi"] = "fi"
    lock_ttl_seconds: PositiveInt = 600


class LeagueSettings(_Section):
    """``[league]`` -- Pappaliigan kausi, karttapooli ja sääntökirjan arvot.

    Luetaan ``select``-, ``classify``- ja ``aggregate``-vaiheissa. **Ei
    ``render``issä**: raportti saa liigatiedon ``report.json``ista, ja
    vaiheen ainoa oma osio on ``[report]`` (Story 2.13). Väite oli täällä
    ennen sitä, ja se oli väärä jo silloin -- vaihe ei ottanut yhtäkään
    asetusosiota parametrikseen.
    """

    season: PositiveInt
    organizer_id: str
    championship_ids: list[str] = Field(min_length=1)
    map_pool: list[str] = Field(min_length=1)
    own_default_bans: list[str] = Field(default_factory=list)
    mr: PositiveInt = 12
    ot_start_money: PositiveInt = 12500

    @model_validator(mode="after")
    def _check_bans_are_in_pool(self) -> "LeagueSettings":
        outside = [m for m in self.own_default_bans if m not in self.map_pool]
        if outside:
            raise ValueError(
                "own_default_bans sisältää kartan, jota ei ole karttapoolissa: "
                f"{', '.join(outside)}. Poistuiko kartta Active Dutysta?"
            )
        return self


class ParseSettings(_Section):
    """``[parse]`` -- ainoa osio, jonka ``parse``-vaihe näkee.

    ``parse``-manifestin parametrihash lasketaan vain tästä osiosta ja
    demoparser2:n versiosta, joten kynnysten säätö ei aiheuta uudelleenparsintaa.
    """

    snapshot_seconds: list[float] = Field(min_length=1)
    #: Ostoajan pituus sekunteina freezetimen päättymisestä. Talouden
    #: mittauspiste on::
    #:
    #:     max(ankkuri,
    #:         min(ankkuri + tämä,
    #:             ensimmäistä kuolemaa EDELTÄVÄ tick,
    #:             kierroksen loppu))
    #:
    #: eikä freezetimen loppu: CS2:n ostoaika jatkuu kierroksen alettua, ja
    #: noin puolella kierroksista ostetaan vielä sen jälkeen. Kuolemaa
    #: **edeltävä** tick eikä kuoleman tick, koska kuolleen tavaraluettelo on
    #: jo tyhjä; uloin ``max`` on alaraja, joka estää mittauspistettä valumasta
    #: freezetimen sisään.
    #:
    #: Oletus 20,0 on **linjaus, jonka mittaus tukee** eikä kalibroitu arvo --
    #: perustelu, mittaukset ja niiden rajat ovat ``settings.toml``in
    #: rivikommentissa. Sallittu väli 0..:data:`MAX_BUY_WINDOW_SECONDS`;
    #: 0 tarkoittaa "mittaa ankkurista".
    buy_window_seconds: float = 20.0
    #: Aseet, jotka eivät kelpaa ensikontaktiksi (AD-5: "ase ei ole utility").
    first_contact_exclude_weapons: list[str] = Field(default_factory=list)
    #: Jos ensikontaktia ei löydy player_hurt-tapahtumista, käytetäänkö
    #: ensimmäistä player_death-tapahtumaa.
    first_contact_fallback_death: bool = True
    #: Enimmäisetäisyys pelin yksiköissä, jolta räjähdyksen alue saa tulla
    #: **lähimmästä pistepilviruudusta** (Story 2.9).
    #:
    #: **Pakollinen, ei valinnainen.** Se oli ``None``-oletuksellinen niin
    #: kauan kuin alue napsautettiin lähimmästä pelaajasta ja napsautus oli
    #: kytkettävissä pois. Pistepilvestä lähin ruutu löytyy **aina**, joten
    #: kynnyksetön ajo antaisi jokaiselle räjähdykselle alueen etäisyydestä
    #: riippumatta ja kattavuus olisi aina 100 % -- se ei olisi kattavuutta
    #: vaan mittarin puuttuminen. Speksin Always-sääntö on "etäisyyskynnys
    #: säilyy", ja pakollinen kenttä tekee siitä rakenteellisen.
    #:
    #: Mitta on painotettu samoin kuin nimeäminen (ks. kolme seuraavaa), joten
    #: se ei ole euklidinen etäisyys vaan se luku, jolla lähin ruutu valittiin.
    #: Etäisyys tallentuu ``EVENTS.snap_distance``iin myös silloin, kun se
    #: ylittää kynnyksen.
    area_snap_units: PositiveInt
    #: Pistepilven ruudun särmä pelin yksiköissä. Mitattu 2026-08-30 kahdella
    #: demolla: 32 puolittaa mediaanietäisyyden 64:ään verrattuna (15 vs. 29).
    #: Ruutu 64 kattaa hieman enemmän (Ancient 93,2 % vs. 91,8 %), mutta pieni
    #: etäisyys on se, mikä tekee nimetystä alueesta todennäköisesti oikean.
    #: Ruutuja on 32:lla noin 7 700-10 500 per demo. Rajat: ks.
    #: :data:`CalloutGridUnits`.
    callout_grid_units: CalloutGridUnits = 32
    #: Pystyeron painokerroin, kun räjähdykselle etsitään lähintä ruutua.
    #: Kerroskartat (Nuke) vaativat sen: alakerran ruutu on ylhäältä katsoen
    #: aivan vieressä mutta eri alueella. **Vain toleranssin kanssa** -- ks.
    #: seuraava.
    #:
    #: Oletus on **1.0 eikä 2.0**: mitattuna paino 1 erottaa kerrokset
    #: täydellisesti (nolla väärää kerrosta molemmilla Nuke-demoilla), ja
    #: jokainen sitä suurempi paino maksaa kattavuutta ostamatta mitään --
    #: 99,0 % painolla 1, 98,8 % painolla 2, 97,4 % painolla 3.
    callout_z_weight: NonNegativeFloat = 1.0
    #: Pystyero, joka on painotuksessa ilmaista. Pelaajan korkeus 72 yksikköä:
    #: pistepilvi tallentaa pelaajan sijainnin, mutta kranaatti räjähtää
    #: mistä tahansa lattian ja pään väliltä (savu ilmassa, molotov
    #: lattialla), joten ilman toleranssia pystyrangaistus osuisi normaaliin
    #: tapaukseen. Mitattu: pelkkä paino ilman toleranssia on **huonompi**
    #: kuin ei painoa lainkaan (mediaani 30 vs. 20); toleranssin kanssa
    #: mediaani putoaa 15:een.
    #:
    #: **Symmetrinen, ja se on mitattu.** Vapaus vain ylöspäin (savu leijuu)
    #: on mitattuna huonompi: mediaani nousee 15 -> 17 (Ancient) ja
    #: 14 -> 19 (Nuke) eikä kattavuus parane lainkaan.
    callout_z_tolerance_units: NonNegativeFloat = 72.0

    # Kalustolaskurilla (``players_armed_buy_end``) **ei ole asetusta**.
    # Story 1.5:n ``armed_player_equip_min`` mittasi varustearvoa, joka on ase
    # + panssari + kranaatit yhtenä lukuna; Story 1.6 vaihtoi mittarin
    # havaintoon "panssari ja vähintään yksi ase hallussa", ja aseluettelo on
    # koodissa (:mod:`pappascout.constants`) eikä täällä. Luettelon muutos
    # mitätöi arkiston silti, koska ``stages.parse`` ottaa siitä tiivisteen
    # parametrihashiin -- käyttäjä ei säädä 57 esinenimeä.
    #
    # Laskuri mittaa **hallussapitoa, ei ostosta**: säästetty tai poimittu
    # kivääri laskeutuu samoin kuin ostettu, koska kierroksen kannalta
    # ratkaisee mitä kädessä on. Ks. :mod:`pappascout.constants`.

    @model_validator(mode="after")
    def _check_buy_window(self) -> "ParseSettings":
        """Ostoikkuna on asetus, joten se on tarkistettava latauksessa.

        Kolme tapaa mennä rikki hiljaa:

        * **NaN tai ääretön** kaataisi ``round()``-kutsun kesken parsinnan,
          satojen megatavujen lukemisen jälkeen.
        * **Negatiivinen** siirtäisi mittauspisteen freezetimen sisään, eli
          ennen kuin joukkue on edes ostanut. Nolla on sallittu ja tarkoittaa
          "mittaa freezetimen lopusta" -- se on tämän tarinan edeltävä
          käyttäytyminen, ja se on kelvollinen valinta.
        * **Ylärajan ylittävä** ikkuna ei mittaisi mitään uutta: mittauspiste
          rajautuu joka tapauksessa kierroksen loppuun ja ensimmäiseen
          kuolemaan, joten liian iso arvo on lähes varmasti kirjoitusvirhe.
          Raja on :data:`MAX_BUY_WINDOW_SECONDS` eikä näytepisteiden raja.
        """
        value = self.buy_window_seconds
        # isfinite ENSIN eikä vertailujen jälkeen: nan on pienempi, suurempi
        # ja yhtä suuri kuin mikä tahansa -- kaikki False. Vertailuista
        # koostuva tarkistus päästäisi sen läpi, ja round(nan * tick_rate)
        # kaatuisi vasta parsinnan sisällä, sen jälkeen kun 400 MB:n demo on
        # jo purettu ja luettu.
        if not isfinite(value):
            raise ValueError(
                f"buy_window_seconds on {value!r}, joka ei ole äärellinen luku. "
                "Ostoikkuna kerrotaan tickratella parsinnan aikana, joten "
                "nan ja ääretön kaatuisivat vasta demon lukemisen jälkeen."
            )
        if value < 0:
            raise ValueError(
                f"buy_window_seconds on {value:g}, joka on negatiivinen. "
                "Ostoaika mitataan freezetimen lopusta eteenpäin; negatiivinen "
                "arvo siirtäisi mittauspisteen freezetimen sisään."
            )
        if value > MAX_BUY_WINDOW_SECONDS:
            raise ValueError(
                f"buy_window_seconds on {value:g} s, joka ylittää ostoikkunan "
                f"ylärajan ({MAX_BUY_WINDOW_SECONDS:g} s eli kaksi kertaa "
                "pelin oma ostoaika). Mittauspiste rajautuu joka tapauksessa "
                "kierroksen loppuun ja ensimmäiseen kuolemaan, joten sitä "
                "suurempi arvo ei mittaisi mitään uutta."
            )
        return self

    @model_validator(mode="after")
    def _check_snapshot_seconds(self) -> "ParseSettings":
        """Näytepisteajat ovat asetus, joten ne on tarkistettava latauksessa.

        Kolme tapaa mennä rikki hiljaa:

        * **Tyhjä lista** pääsi läpi ``min_length=1``-tarkistuksesta vain, jos
          avain puuttuu kokonaan -- mutta ``[]`` on eri asia: silloin taulussa
          olisi pelkkiä ensikontakteja, ja virheellinen konfiguraatio näyttäisi
          onnistuneelta ajolta.
        * **NaN tai ääretön** kaataisi ``round()``-kutsun kesken parsinnan,
          satojen megatavujen lukemisen jälkeen.
        * **Kirjoitusvirhe** kuten ``450.0`` (tarkoitettu ``45.0``) ei kaataisi
          mitään: piste vain putoaisi jokaiselta kierrokselta, ja taulu olisi
          hiljaa vajaa.
        """
        if not self.snapshot_seconds:
            raise ValueError(
                "snapshot_seconds on tyhjä. Ilman näytepisteitä asetelmataulu "
                "jäisi pelkkien ensikontaktien varaan."
            )
        for value in self.snapshot_seconds:
            if not isfinite(value):
                raise ValueError(
                    f"snapshot_seconds sisältää arvon {value!r}, joka ei ole "
                    "äärellinen luku."
                )
            if value <= 0:
                raise ValueError(
                    f"snapshot_seconds sisältää arvon {value:g}, joka ei ole "
                    "positiivinen. Näytepisteet mitataan freezetimen lopusta "
                    "eteenpäin; nolla olisi ankkuri itse, jossa kukaan ei ole "
                    "vielä liikkunut."
                )
            if value > MAX_SNAPSHOT_SECONDS:
                raise ValueError(
                    f"snapshot_seconds sisältää arvon {value:g} s, joka ylittää "
                    f"kierroksen keston ({MAX_SNAPSHOT_SECONDS:g} s). Piste "
                    "putoaisi jokaiselta kierrokselta, joten se on lähes "
                    "varmasti kirjoitusvirhe."
                )
        duplicates = sorted(
            {a for a in self.snapshot_seconds if self.snapshot_seconds.count(a) > 1}
        )
        if duplicates:
            raise ValueError(
                "snapshot_seconds sisältää saman ajan kahdesti: "
                f"{', '.join(f'{a:g}' for a in duplicates)}."
            )
        return self


class ThresholdSettings(_Section):
    """``[thresholds]`` -- kaikki luokittelun ja otannan rajat.

    Luokittelun kynnykset on kalibroitu 2026-08-29 ihmisen antamaa
    totuustaulua vasten (``kalibrointi-kierrostyypit.md``); perustelut ja
    havaintoväli ovat ``settings.toml``in kommenteissa.

    **Kaikki eivät ole havaintoja.** Erottelu on ``settings.toml``issa
    rivikohtaisena merkintänä, ja se on tarkoitus säilyttää:

    * ``[kalibroitu]`` -- arvo on aineiston tyhjässä välissä ja marginaali
      lähimpään havaintoon on mitattu (``full_equip_min``, ``force_buy_min``,
      ``anomaly_equip_max_after_win``, ``normal_buy_money_min``).
    * ``[lausuttu]`` -- arvo tulee käyttäjän sanomasta säännöstä, eikä
      yksikään mitattu kierros koettele sitä (``armed_players_min``).
    * ``[päätelty, odottaa havaintoa]`` -- arvo on päättelyä, ja aineisto
      antaisi saman tuloksen laajalla välillä (``normal_buy_players_min``).

    Otannan rajat (``small_sample_rounds``, ``stack_min_players``,
    ``roster_*``) odottavat yhä omaa aineistoaan.

    Story 2.5:n poikkeamakynnykset (``advance_*``, ``crunch_*``) ovat
    ``[kalibroitu]``: jokainen on mitattu kahdeksalla demolla ja kahdella eri
    joukkueella (``kalibrointi-ct-eteneminen.md``), ja niiden osumatiheys on
    poikkeaman kokoluokkaa eikä normaalia pelaamista. ``stack_min_players``
    jää käyttämättä -- stack-sääntö on mitattu mahdottomaksi nykyisellä
    aluejaolla ja siirretty omaksi tarinakseen.

    Rahamäärät ovat dollareita **per pelaaja**, paitsi
    ``normal_buy_money_min``, joka on **yhden pelaajan** oma saldo eikä
    joukkueen keskiarvo.
    """

    # Kierrosnumeroon perustuvat säännöt (AD-4 vaiheet 1 ja 2)
    pistol_rounds: list[PositiveInt] = Field(min_length=1)
    regulation_rounds: PositiveInt = 24

    # Varustearvorajat (AD-4 vaiheet 3 ja 5)
    full_equip_min: PositiveInt = 4000
    anomaly_equip_max_after_win: PositiveInt = 2000

    # Raharajat (AD-4 vaihe 4). Osto on kaikkien häviön jälkeisten luokkien
    # yhteinen edellytys: force_buy_min on forcen ja puolioston ehto, ei vain
    # niiden kaista.
    force_buy_min: PositiveInt = 1500

    # Puolioston kaksi ehtoa (Story 1.10). MOLEMPIEN on täytyttävä.
    #
    # A: montako pelaajaa oli aseistettu -- erottaa puolioston ECOSTA.
    # B: montako pelaajaa pystyy normaaliin ostoon seuraavalla kierroksella,
    #    kun taskuun jääneeseen rahaan lisätään häviöbonus -- erottaa
    #    puolioston FORCESTA.
    #
    # normal_buy_money_min on YHDEN PELAAJAN oma saldo, ei joukkueen keskiarvo.
    # Ero on koko säännön syy: keskiarvo peittää jakauman.
    armed_players_min: PositiveInt = 3
    normal_buy_money_min: PositiveInt = 4000
    normal_buy_players_min: PositiveInt = 3

    # Loss count -säännöt
    loss_count_half_start: NonNegativeInt = 1
    loss_count_min: NonNegativeInt = 0
    loss_count_max: PositiveInt = 4

    # Joukkueidentiteetti ja rosterikynnys (AD-6)
    team_identity_min_common: PositiveInt = 3
    roster_size: PositiveInt = 5
    roster_min_regulars: PositiveInt = 4

    # Otanta ja poikkeamat (AD-10)
    small_sample_rounds: PositiveInt = 3

    # Stack-sääntö (Story 2.14). stack_min_players oli tässä KÄYTTÄMÄTTÖMÄNÄ
    # Story 2.5:stä lähtien; se otettiin käyttöön vasta kun puuttuva pala --
    # kuvaus alue -> alueryhmä -- saatiin johdettua demon omasta
    # pistepilvestä (domain.sampling.site_groups).
    #
    # Kaikki kolme ovat MITATTUJA eivätkä valittuja: perustelu arvo arvolta on
    # settings.tomlin kommenteissa ja kokonaisuudessaan
    # kalibrointi-stack.md:ssä.
    #
    # Molemmat uudet ovat SUHDELUKUJA eivätkä pelin yksiköitä: kumpikin
    # verrataan kahden etäisyyden osamäärään, joten pistepilven ruudukon koko
    # ([parse].callout_grid_units) supistuu niistä pois. Juuri siksi
    # aggregointi ei lue [parse]-osiota tämän säännön takia. Jos joskus
    # tarvitaan absoluuttinen etäisyysraja, se EI voi olla tällainen luku:
    # ruutuindeksi ei ole koordinaatti, vaan se on ensin kerrottava ruudun
    # koolla.
    stack_min_players: PositiveInt = 4
    # Alaraja 1,0 on määritelmä eikä säädin: marginaali kertoo, kuinka paljon
    # kauempana toisen siten on oltava ennen kuin alue luetaan lähemmän
    # ryhmään, ja arvolla alle 1 "lähempi" site voisi olla kauempana. Tasan
    # 1,0 on kelvollinen: silloin jokainen alue kuuluu lähempään siteen eikä
    # yksikään jää jaetuksi.
    stack_group_margin: Annotated[
        float, Field(ge=1.0, le=MAX_STACK_GROUP_MARGIN, allow_inf_nan=False)
    ] = 1.25
    # Alaraja on yli 0: arvolla 0 vartija ei vaientaisi yhtäkään karttaa, eli
    # Nuken päällekkäisistä siteistä johdettaisiin aluejako, jota ei ole
    # olemassa -- ja sääntö raportoisi Nuken kierrokset tutkituiksi.
    stack_site_separation_min: Annotated[
        float,
        Field(gt=0.0, le=MAX_STACK_SITE_SEPARATION, allow_inf_nan=False),
    ] = 2.0

    # Poikkeamasäännöt (Story 2.5). Kaikki kuusi ovat MITATTUJA, eivät
    # valittuja: perustelu arvo arvolta on settings.tomlin kommenteissa ja
    # kokonaisuudessaan kalibrointi-ct-eteneminen.md:ssä.
    #
    # advance_t_share on YHTEINEN molemmille säännöille: molemmat kysyvät
    # saman "alue on T:n hallussa" -ehdon, ja kahdella laskennalla ne voisivat
    # olla eri mieltä siitä, kumman aluetta alue on. Crunch lisää ehtoon
    # suuntavaatimuksen mutta pudottaa kierrostyyppirajauksen, joten se ei ole
    # etenemisen tiukempi muoto vaan eri rajaus samasta havainnosta.
    advance_t_share: MajorityShare = 0.80
    advance_area_min_observations: PositiveInt = 20
    advance_max_sample_s: Annotated[
        float, Field(gt=0.0, le=MAX_ADVANCE_SAMPLE_SECONDS, allow_inf_nan=False)
    ] = 30.0
    advance_min_players: PositiveInt = 1
    # Alaraja 2 on sääntö eikä säädin: crunch on määritelmällisesti
    # saapumista **useasta** suunnasta ("kahdesta tai useammasta suunnasta
    # samaan aikaan"), ja arvolla 1 sekä sääntö että raportin lukuohje
    # väittäisivät jotain, mitä koodi ei enää vaadi.
    crunch_min_players: PositiveInt = 2
    crunch_min_sources: Annotated[int, Field(ge=2)] = 2

    @model_validator(mode="after")
    def _check_ranges_are_consistent(self) -> "ThresholdSettings":
        if self.anomaly_equip_max_after_win >= self.full_equip_min:
            raise ValueError(
                f"anomaly_equip_max_after_win ({self.anomaly_equip_max_after_win}) "
                f"on oltava pienempi kuin full_equip_min ({self.full_equip_min}); "
                "muuten voiton jälkeinen poikkeamaraja söisi täyden oston, ja "
                "jokainen voitettu kierros olisi joko poikkeama tai täysi osto "
                "sen mukaan, kumpi raja sattuu olemaan ylempänä."
            )
        # Pelaajamääräkynnykset verrataan **kentällä olevien** määrään eikä
        # rosterin kokoon. Ne eroavat: rosterissa voi olla vaihtopelaajia
        # (mitattu: seitsemän pelaajaa yhdellä joukkueella), mutta kentällä on
        # aina viisi, joten kuusi aseistettua tai kuusi pelaajaa alueella on
        # mahdoton ehto vaikka roster_size sen päästäisi läpi.
        on_server = min(self.roster_size, PLAYERS_ON_SERVER)
        for name in (
            "armed_players_min",
            "normal_buy_players_min",
            "advance_min_players",
            "crunch_min_players",
            # Stack on ainoa kynnys, joka on mitattu KIINNI ylärajaan: neljä
            # on kalibroitu ja viisi on aito ääripää (2 kierrosta 66:sta),
            # joten viisi on kelvollinen arvo eikä vartija saa hylätä sitä.
            # Kuusi puolustajaa sen sijaan on mahdoton havainto.
            "stack_min_players",
        ):
            value = getattr(self, name)
            if value > on_server:
                raise ValueError(
                    f"{name} ({value}) on suurempi kuin kentällä olevien "
                    f"pelaajien määrä ({on_server}), joten ehto ei voi "
                    "täyttyä yhdelläkään kierroksella eikä sääntö voisi "
                    "koskaan laueta."
                )
        if self.force_buy_min >= self.full_equip_min:
            raise ValueError(
                f"force_buy_min ({self.force_buy_min}) on oltava pienempi kuin "
                f"full_equip_min ({self.full_equip_min}); muuten jokainen "
                "forcen ehdon täyttävä ostos nostaisi varustearvon jo täyden "
                "oston rajalle, eikä forcea eikä puoliostoa voisi koskaan "
                "saavuttaa."
            )
        if self.loss_count_min >= self.loss_count_max:
            raise ValueError(
                f"loss_count_min ({self.loss_count_min}) on oltava pienempi kuin "
                f"loss_count_max ({self.loss_count_max})."
            )
        if not (
            self.loss_count_min
            <= self.loss_count_half_start
            <= self.loss_count_max
        ):
            raise ValueError(
                f"loss_count_half_start ({self.loss_count_half_start}) on "
                f"rajojen {self.loss_count_min}-{self.loss_count_max} ulkopuolella."
            )
        if self.roster_min_regulars > self.roster_size:
            raise ValueError(
                f"roster_min_regulars ({self.roster_min_regulars}) ei voi olla "
                f"suurempi kuin roster_size ({self.roster_size})."
            )
        # HUOM: crunch_min_players ja advance_min_players EIVÄT ole
        # järjestyksessä toisiinsa nähden, eikä sellaista vartijaa saa lisätä.
        # Crunch ei ole etenemisen tiukempi muoto: se on tiukempi suunnista ja
        # löysempi kierrostyypistä, joten kumpikaan osumajoukko ei sisällä
        # toista (mitattu: MatureMayhem Anubis k10 on täysi osto, jolla
        # etenemistä ei ole olemassa). Vertailu näiden kahden välillä olisi
        # siis sääntö ilman perustetta.
        if self.crunch_min_sources > self.crunch_min_players:
            raise ValueError(
                f"crunch_min_sources ({self.crunch_min_sources}) on suurempi "
                f"kuin crunch_min_players ({self.crunch_min_players}). "
                "Jokainen lähtösuunta tarvitsee oman pelaajansa, joten ehto "
                "ei voisi täyttyä yhdelläkään näytepisteellä."
            )
        return self


class AggregateSettings(_Section):
    """``[aggregate]`` -- vain ``aggregate``-vaiheen omat arvot (AD-3).

    **Miksi oma osio eikä ``[thresholds]``.** ``classify`` laskee
    parametrihashinsa koko ``[thresholds]``-osiosta, koska osittainen hash
    vaatisi listan siitä, mitä kenttiä säännöt sattuvat lukemaan -- ja se
    lista vanhenisi hiljaa. Hinta on, että mikä tahansa lisäys kyseiseen
    osioon mitätöi jokaisen luokitellun demon. Utilityn aikaikkunat ovat
    puhtaasti aggregoinnin esitysvalinta, eikä ``classify`` lue niitä
    lainkaan, joten niiden säätäminen ei saa pakottaa uudelleenluokittelua.
    Osiointi tekee siitä rakenteellisen: ``classify`` ei näe tätä osiota.
    """

    #: Aikaikkunoiden rajat sekunteina kierroksen alusta. Rajat ``[5, 10, 20]``
    #: tuottavat lokerot ``0-5``, ``5-10``, ``10-20`` ja ``20+``; raja kuuluu
    #: aina ylempään lokeroon. Oletus on **sama kuin settings.tomlissa**:
    #: tyhjä oletus tuottaisi hiljaa yhden lokeron raportin, jos avain
    #: unohtuisi tiedostosta.
    utility_seconds_buckets: list[float] = Field(
        default_factory=lambda: [5.0, 10.0, 20.0]
    )

    @model_validator(mode="after")
    def _check_utility_buckets(self) -> "AggregateSettings":
        """Aikaikkunat ovat asetus, joten ne tarkistetaan latauksessa.

        Neljä tapaa mennä rikki hiljaa:

        * **NaN tai ääretön** liukuisi vertailujen läpi ja päätyisi lokeron
          nimeen, joka ei tarkoita mitään.
        * **Negatiivinen tai nolla** raja tuottaisi lokeron, johon ei voi osua
          yksikään heitto: ``t_s`` mitataan freezetimen lopusta eteenpäin.
        * **Järjestämätön tai toistuva** lista tuottaisi lokeron, jonka
          alaraja on ylärajaa suurempi -- se ei kaatuisi, vaan jäisi tyhjäksi
          ja veisi heitot naapurilokeroon.
        * **Kaksi rajaa, jotka näyttävät samalta nimessä** (esimerkiksi
          ``5.0000001`` ja ``5.0000002``) tuottaisivat kaksi lokeroa samalla
          nimellä, jolloin raportin rivi olisi monitulkintainen. Nimi
          muotoillaan lyhimpään esitysmuotoon, joten tarkistus tehdään
          nimistä eikä luvuista.

        Tyhjä lista on sallittu ja tarkoittaa yhtä lokeroa (``kaikki``):
        aikaikkunan poistaminen on kelvollinen valinta eikä sen tarvitse olla
        koodimuutos.
        """
        previous: float | None = None
        for value in self.utility_seconds_buckets:
            if not isfinite(value):
                raise ValueError(
                    f"utility_seconds_buckets sisältää arvon {value!r}, joka "
                    "ei ole äärellinen luku."
                )
            if value <= 0:
                raise ValueError(
                    f"utility_seconds_buckets sisältää arvon {value:g}, joka "
                    "ei ole positiivinen. Aikaikkunat mitataan freezetimen "
                    "lopusta eteenpäin, joten nolla olisi ensimmäisen lokeron "
                    "alaraja eikä sen yläraja."
                )
            if previous is not None and value <= previous:
                raise ValueError(
                    "utility_seconds_buckets on oltava aidosti kasvava; "
                    f"{value:g} tulee arvon {previous:g} jälkeen. "
                    "Järjestämättömässä listassa olisi lokero, jonka alaraja "
                    "on ylärajaa suurempi -- se jäisi hiljaa tyhjäksi."
                )
            previous = value
        names = [f"{v:g}" for v in self.utility_seconds_buckets]
        if len(names) != len(set(names)):
            raise ValueError(
                "utility_seconds_buckets sisältää kaksi rajaa, jotka "
                f"näyttävät samalta lokeron nimessä ({', '.join(names)}). "
                "Nimi muotoillaan lyhimpään esitysmuotoon, joten kaksi "
                "lähekkäistä arvoa tuottaisi kaksi lokeroa samalla nimellä."
            )
        return self


class ReportSettings(_Section):
    """``[report]`` -- karsintasäännöt: mitä raportti jättää kirjoittamatta.

    Story 2.13. Raportti oli **noin 96 sisältöriviä karttaa kohden**, Veetin
    oma analyysi noin 30, ja osa pituudesta oli puhdasta toistoa. Viisi
    sääntöä jättää sen kirjoittamatta.

    **Mitatut perusteet ja luvut ovat ``settings.toml``issa**, eivät täällä.
    Ne ovat mittaustuloksia, joita säätävä ihminen lukee säätäessään arvoa, ja
    kolmeen paikkaan kirjoitettuina kaksi kopiota vanhenee hiljaa. Täällä on
    vain se, mikä on koodin sopimusta.

    **Miksi oma osio eikä ``[aggregate]``.** Osiointi seuraa vaihetta, joka
    arvot **lukee** (AD-3), ja nämä lukee ``render``. ``[aggregate]`` on
    kokonaisena ``aggregate``-vaiheen parametrihashissa, joten sinne
    kirjoitettu esitysvalinta mitätöisi jokaisen aggregoinnin -- eli
    esitysasetuksen kääntäminen pakottaisi laskemaan ``report.json``in
    uudelleen, vaikka sen sisältö ei muutu. Oma osio tekee lupauksesta
    "karsinta koskee esitystä eikä sisältöä" rakenteellisen.

    **Jokainen sääntö on oma asetus**, jotta mikä tahansa niistä voidaan
    kääntää pois ilman koodimuutosta -- ja jotta ne kaikki pois käännettyinä
    raportti on merkki merkiltä sama kuin ennen tätä tarinaa. Oletukset ovat
    ``settings.toml``in oletukset: koodioletus, joka eroaisi tiedostosta,
    karsisi hiljaa eri tavalla silloin kun avain on unohtunut tiedostosta.

    **Karsinta ei muuta yhtäkään lukua** -- ei ``report.json``issa eikä
    raportissa. Erityisesti se ei muuta lohkon kuviosuodatuksen kirjanpitoa:
    rivi rakennetaan aina ensin ja karsitaan vasta sitten
    (:mod:`pappascout.render.view`).

    **Osa kierrostyypeistä on suojattu kaikilta säännöiltä**, ja luettelon
    omistaa :data:`pappascout.render.view.PROTECTED_ROUND_TYPES`, koska se on
    esitysvalinta eikä säädettävä arvo.

    **Vaatimus uudelle kentälle: epätosi arvo tarkoittaa "sääntö pois."**
    ``False``, tyhjä lista ja ``0`` ovat kaikki "ei karsi", ja renderöinti
    tunnistaa siitä mekaanisesti, onko karsinta lainkaan mukana -- se päättää
    yhteenvedon karsintarivin olemassaolon, eli sen, onko raportti merkki
    merkiltä sama kuin ennen tätä tarinaa. Kenttä, jonka nollan pitäisi
    tarkoittaa jotain muuta, rikkoisi sen ilman että mikään kaatuu.
    """

    #: Sääntö 1: jätä kylläinen kalustorivi kirjoittamatta.
    #:
    #: Kylläinen = jakaumassa on **yksi pylväs**, sen arvo on
    #: :data:`PLAYERS_ON_SERVER` ja havainto saatiin joka kierrokselta.
    drop_saturated_equipment_lines: bool = True

    #: Sääntö 2: kirjoita aseistettujen ja panssaroitujen rivi yhtenä, kun
    #: jakaumat ovat identtiset (pylväät, jakaja ja huomautus).
    merge_equal_equipment_lines: bool = True

    #: Sääntö 3: aikanäytepisteet, joita **ei kirjoiteta** raporttiin.
    #:
    #: Oletus on tyhjä eli sääntö on pois päältä, ja se on mittaustulos:
    #: myöhäinen näytepiste on ohut ja vinoutunut mutta **ei toistoa** kuten
    #: säännöt 1, 2, 4 ja 5, joten pois jättäminen voi maksaa sisältöä. Luvut
    #: ovat ``settings.toml``issa.
    #:
    #: **Ei sama asia kuin** ``[parse].snapshot_seconds``: näytepiste pysyy
    #: taulussa ja ``report.json``issa, kyse on vain siitä tulostetaanko se.
    #: Sekunnit täsmätään :func:`~pappascout.constants.seconds_label`in
    #: muodossa eli sellaisina kuin ne ovat rivin nimiössä, joten ``45`` ja
    #: ``45.0`` tarkoittavat samaa riviä.
    #:
    #: **Lista järjestetään latauksessa**, koska se menee ``render``-vaiheen
    #: parametrihashiin: järjestyksen vaihtaminen tuottaa merkki merkiltä
    #: saman raportin, joten järjestämätön lista antaisi kahdelle
    #: identtiselle raportille eri parametrihashin.
    skip_sample_seconds: list[float] = Field(default_factory=list)

    #: Sääntö 4: enintään näin monta **kohdetta** utilityn kohderivillä.
    #:
    #: Kohde on räjähdysalue eikä väite: sama kohde voi esiintyä rivillä
    #: useammin kuin kerran (eri heittoalue tai eri aikaikkuna), ja rajaus
    #: koskee kohteita -- muuten kaksi säilytettyä väitettä voisi olla sama
    #: kohde kahdessa ikkunassa, ja rivi menettäisi jokaisen muun kohteen
    #: samalla kun huomautus kutsuu niitä kohteiksi.
    #:
    #: ``0`` tarkoittaa "ei rajaa" eli sääntö pois: rivin tyhjentäminen ei ole
    #: karsintaa vaan vaimennus, joka on rajattu tästä tarinasta ulos.
    max_utility_targets: NonNegativeInt = 2

    #: Sääntö 5: enintään näin monta aluetta tapporivillä. ``0`` = ei rajaa.
    #:
    #: Kolme eikä kaksi, koska tapporivi on koko kierrostyypin tapot yhdellä
    #: rivillä ja sen jakauma on leveämpi.
    max_kill_areas: NonNegativeInt = 3

    @field_validator("skip_sample_seconds")
    @classmethod
    def _check_skip_sample_seconds(cls, value: list[float]) -> list[float]:
        """Tarkista näytepistelista ja **järjestä se** latauksessa.

        Kolme tapaa mennä rikki hiljaa, ja kaikki kolme päättyisivät samaan:
        asetus näyttäisi poistavan rivin muttei poistaisi mitään.

        * **NaN tai ääretön** ei täsmää yhteenkään näytepisteeseen.
        * **Negatiivinen tai nolla** ei ole näytepiste: ``t_s`` mitataan
          freezetimen lopusta eteenpäin, ja ``[parse].snapshot_seconds``
          vaatii positiivisen arvon.
        * **Kaksi arvoa, jotka näyttävät samalta rivin nimiössä**
          (``45.0000001``) tarkoittaisivat samaa riviä kahdesti. Tarkistus
          tehdään :func:`~pappascout.constants.seconds_label`illa eli samalla
          funktiolla kuin täsmäys; kahdella muotoilulla ne sopisivat vain
          tänään.

        Järjestäminen on **parametrihashin takia** eikä siisteyttä: ``render``
        hashaa osionsa kokonaisena, ja ``[45, 15]`` tuottaa merkki merkiltä
        saman raportin kuin ``[15, 45]``. Järjestämättömänä manifesti
        väittäisi kahden identtisen raportin syntyneen eri parametreilla.

        Näytepistettä, jota ``[parse].snapshot_seconds``issa ei ole, **ei
        torjuta**: osiot eivät näe toisiaan (AD-3), ja raportti latotaan myös
        vanhoista ``report.json``eista, joissa näytepisteet ovat ne, jotka
        parsinnan aikaan olivat käytössä.
        """
        labels: list[str] = []
        for seconds in value:
            if not isfinite(seconds):
                raise ValueError(
                    f"skip_sample_seconds sisältää arvon {seconds!r}, joka ei "
                    "ole äärellinen luku, eikä se voi täsmätä yhteenkään "
                    "näytepisteeseen."
                )
            if seconds <= 0:
                raise ValueError(
                    f"skip_sample_seconds sisältää arvon "
                    f"{seconds_label(seconds)}, joka ei ole positiivinen. "
                    "Näytepisteet mitataan freezetimen lopusta eteenpäin, "
                    "joten nolla tai negatiivinen ei ole näytepiste."
                )
            labels.append(seconds_label(seconds))
        if len(labels) != len(set(labels)):
            raise ValueError(
                "skip_sample_seconds sisältää saman näytepisteen kahdesti "
                f"({', '.join(labels)}). Sekunnit täsmätään siinä muodossa, "
                "jossa ne ovat rivin nimiössä, joten kaksi lähekkäistä arvoa "
                "tarkoittaisi samaa riviä."
            )
        return sorted(value)


class EconomySettings(_Section):
    """``[economy]`` -- CS2:n talousmalli.

    Yksi näistä osallistuu kierroksen luokitteluun: ``loss_bonus_steps``.
    Puoliosto erotetaan forcesta sillä, pystyykö pelaaja normaaliin ostoon
    seuraavalla kierroksella, ja se riippuu häviöbonuksesta -- joka on suoraan
    loss countin funktio ja vaihtelee 1 400-3 400 $. Siksi ``classify`` saa
    tämän osion ja sen sisältö on mukana vaiheen parametrihashissa (Story
    1.10). Muut arvot selittävät raportissa, miksi joukkueella oli se raha
    joka sillä oli.
    """

    start_money: PositiveInt = 800
    max_money: PositiveInt = 16000
    loss_bonus_steps: list[PositiveInt] = Field(min_length=1)
    win_reward_elimination: PositiveInt = 3250
    win_reward_bomb: PositiveInt = 3500
    plant_bonus_loss: PositiveInt = 600
    plant_reward: PositiveInt = 300
    defuse_reward: PositiveInt = 300
    ct_kill_bonus: NonNegativeInt = 50
    short_handed_bonus: NonNegativeInt = 1000
    #: Tapporaha aseluokittain; poikkeukset ase kerrallaan.
    kill_rewards: dict[str, int] = Field(default_factory=dict)
    #: Ostovalikon hinnat. Käytetään puoliostojen erotteluun raportissa.
    prices: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_loss_bonus_is_ascending(self) -> "EconomySettings":
        steps = self.loss_bonus_steps
        if any(a >= b for a, b in zip(steps, steps[1:])):
            raise ValueError(
                f"loss_bonus_steps ei ole nouseva: {steps}. "
                "Portaiden on kasvettava, koska laskuri kasvaa häviöistä."
            )
        if self.start_money > self.max_money:
            raise ValueError(
                f"start_money ({self.start_money}) on suurempi kuin max_money "
                f"({self.max_money})."
            )
        return self


class FaceitSettings(_Section):
    """``[faceit]`` -- kuljetuksen säädöt: uudelleenyritys, aikakatkaisu, sivu.

    Story 3.1. Osio on olemassa siksi, että **kynnykset ovat asetuksia eivätkä
    koodia** (AD-3). Jokainen arvo tässä on luku, jonka oikea suuruus riippuu
    verkosta ja rajapinnan kuormasta -- ei pappascoutin logiikasta -- ja jonka
    säätäminen ei saa vaatia koodimuutosta.

    **Osiossa ei ole osoitetta eikä avainta.** Rajapinnan osoite ei ole
    säädettävä arvo vaan se, mitä vastaan asiakas on kirjoitettu; se on
    :mod:`pappascout.adapters.faceit`in vakio, jonka testi saa vaihtaa
    parametrilla. Avaimet eivät ole ``settings.toml``issa lainkaan -- ne
    luetaan koneen omasta ``.env``-tiedostosta, ja se on koko
    :meth:`Settings.require_faceit_api_key`in olemassaolon syy.

    **Osio ei ole minkään vaiheen parametrihashissa**, ja se on linjaus.
    Uudelleenyrityksen tai aikakatkaisun säätäminen ei muuta yhtäkään
    vastausta, jonka rajapinta antaa, joten sen ei pidä mitätöidä mitään
    arkistossa. Sama sukulaisuus kuin ``[report]``illa: osiointi seuraa sitä,
    mitä arvo tekee.

    Attributes:
        retry_attempts: Yritysten **kokonaismäärä**, ei uudelleenyritysten
            määrä: ``1`` tarkoittaa "yritä kerran äläkä uudelleen". Koskee
            vain 429- ja 5xx-vastauksia sekä yhteysvirheitä; muu 4xx ei
            korjaannu odottamalla eikä sitä yritetä uudelleen kertaakaan.
        retry_initial_delay_seconds: Ensimmäinen odotus ennen toista yritystä.
            Viive kaksinkertaistuu joka kierroksella.
        retry_max_delay_seconds: Yhden odotuksen katto. Ilman kattoa
            kahdeksas yritys olisi yli kahden minuutin päässä, ja
            aloitusviiveen säätäminen siirtäisi sitä eksponentiaalisesti.
        timeout_seconds: Yhden HTTP-kutsun aikakatkaisu. Aikakatkaisu on
            yhteysvirhe, eli se **yritetään uudelleen** -- se on tavallisin
            ohimenevä vika.
        page_size: Montako riviä yhdessä sivussa pyydetään. Ylläpitäjän
            säädettävissä, koska sopiva arvo riippuu vastauksen koosta, mutta
            enintään :data:`MAX_FACEIT_PAGE_SIZE`.
        retry_jitter_share: Satunnaisheiton osuus odotuksesta (0.0-1.0).
            Odotus on ``viive * (1 + tämä * satunnaisluku)``. Ilman heittoa
            kaksi rinnakkaista ajoa törmäisi rajoitukseen samalla sekunnilla
            uudelleen ja uudelleen -- eksponentiaalinen viive on molemmilla
            sama funktio samasta hetkestä. ``0.0`` = ei heittoa.
        call_budget_seconds: **Yhden porttikutsun aikabudjetti sekunteina**:
            koko sivutus ja kaikki uudelleenyritykset yhteensä.
            ``retry_attempts`` ei ole katto ajalle, koska sivutus kertoo
            yritykset sivujen määrällä -- pelkkä yrityskatto sallisi satoja
            pyyntöjä ja tunnin hiljaisuuden yhdestä ``get_matches``ista, eli
            juuri sen, minkä :data:`MAX_FACEIT_RETRY_ATTEMPTS` sanoo
            estävänsä. Katto on siis oltava sekunneissa, koska sekunteja
            käyttäjä odottaa.
    """

    retry_attempts: Annotated[int, Field(ge=1, le=MAX_FACEIT_RETRY_ATTEMPTS)] = 4
    retry_initial_delay_seconds: Annotated[
        float, Field(gt=0.0, allow_inf_nan=False)
    ] = 1.0
    retry_max_delay_seconds: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 30.0
    timeout_seconds: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 30.0
    page_size: Annotated[int, Field(ge=1, le=MAX_FACEIT_PAGE_SIZE)] = 100
    retry_jitter_share: Annotated[
        float, Field(ge=0.0, le=1.0, allow_inf_nan=False)
    ] = 0.25
    call_budget_seconds: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 300.0

    @model_validator(mode="after")
    def _check_delays_agree(self) -> "FaceitSettings":
        if self.retry_max_delay_seconds < self.retry_initial_delay_seconds:
            raise ValueError(
                f"retry_max_delay_seconds ({self.retry_max_delay_seconds:g} s) on "
                f"pienempi kuin retry_initial_delay_seconds "
                f"({self.retry_initial_delay_seconds:g} s), joten katto leikkaisi "
                "jo ensimmäisen odotuksen eikä aloitusviive tarkoittaisi mitään."
            )
        # Budjetti on koko kutsun katto, joten yhtä odotusta tai yhtä
        # aikakatkaisua pienempi budjetti pysäyttäisi haun ennen kuin mitään
        # ehtii tapahtua -- uudelleenyritys näyttäisi asetetulta muttei
        # tapahtuisi koskaan.
        if self.call_budget_seconds < self.retry_max_delay_seconds:
            raise ValueError(
                f"call_budget_seconds ({self.call_budget_seconds:g} s) on pienempi "
                f"kuin retry_max_delay_seconds "
                f"({self.retry_max_delay_seconds:g} s), joten budjetti loppuisi "
                "kesken yhden odotuksen eikä yhtäkään uudelleenyritystä voisi "
                "tapahtua."
            )
        if self.call_budget_seconds < self.timeout_seconds:
            raise ValueError(
                f"call_budget_seconds ({self.call_budget_seconds:g} s) on pienempi "
                f"kuin timeout_seconds ({self.timeout_seconds:g} s), joten budjetti "
                "loppuisi ennen kuin yksikään kutsu ehtii aikakatkaista."
            )
        return self


class Settings(BaseSettings):
    """Koko asetuskokonaisuus: kahdeksan osiota ja koneen omat avaimet.

    Vaiheelle ei anneta tätä oliota vaan yksi osio kerrallaan (AD-3).
    """

    # extra="ignore": koneen .env saa sisältää muutakin kuin nämä avaimet ilman
    # että lataus kaatuu. Asetustiedoston tuntemattomat osiot tarkistetaan
    # erikseen load_settings-funktiossa, jotta kirjoitusvirhe ei mene hiljaa läpi.
    model_config = SettingsConfigDict(
        extra="ignore",
        frozen=True,
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    project: ProjectSettings
    league: LeagueSettings
    parse: ParseSettings
    thresholds: ThresholdSettings
    aggregate: AggregateSettings
    report: ReportSettings
    economy: EconomySettings
    faceit: FaceitSettings

    faceit_api_key: SecretStr | None = None
    faceit_downloads_token: SecretStr | None = None

    #: Mistä avaimet luettiin -- vain virheilmoituksia ja ``info``-komentoa varten.
    secrets_file: Path | None = None
    #: Mistä asetustiedosto luettiin.
    settings_file: Path | None = None

    @model_validator(mode="after")
    def _check_sections_agree(self) -> "Settings":
        """Osioiden väliset ristiriidat kiinni jo latausvaiheessa.

        Nämä ovat eri osioissa mutta kuvaavat samaa asiaa: ottelun formaattia.
        Jos ne eroavat, luokittelu tuottaisi hiljaa vääriä kierrostyyppejä koko
        arkiston läpi.
        """
        expected_regulation = 2 * self.league.mr
        if self.thresholds.regulation_rounds != expected_regulation:
            raise ValueError(
                f"thresholds.regulation_rounds ({self.thresholds.regulation_rounds}) "
                f"ei vastaa liigan formaattia MR{self.league.mr}, joka tarkoittaa "
                f"{expected_regulation} säännönmukaista kierrosta."
            )
        expected_pistols = [1, self.league.mr + 1]
        if self.thresholds.pistol_rounds != expected_pistols:
            raise ValueError(
                f"thresholds.pistol_rounds ({self.thresholds.pistol_rounds}) ei "
                f"vastaa liigan formaattia MR{self.league.mr}: pistoolikierrokset "
                f"ovat {expected_pistols} (kierros 1 ja puoliajan 1. kierros)."
            )
        expected_steps = self.thresholds.loss_count_max + 1
        if len(self.economy.loss_bonus_steps) != expected_steps:
            raise ValueError(
                f"economy.loss_bonus_steps sisältää "
                f"{len(self.economy.loss_bonus_steps)} porrasta, mutta loss count "
                f"vaihtelee välillä {self.thresholds.loss_count_min}-"
                f"{self.thresholds.loss_count_max} eli portaita tarvitaan "
                f"{expected_steps}. Laskuri indeksoi tätä listaa suoraan."
            )
        # Puolioston ehto B vertaa "oma saldo + häviöbonus" -summaa arvoon
        # normal_buy_money_min, ja summa katkaistaan rahakattoon. Molemmat
        # rajat ovat [economy]-osiossa, joten saavutettavuutta ei voi
        # tarkistaa kummankaan osion sisällä yksin -- ja ilman tarkistusta
        # kumpi tahansa luokka katoaisi äänettömästi.
        # Poikkeaman aikaraja valikoi NÄYTEPISTEISTÄ, jotka [parse] päättää.
        # Raja, joka on pienempi kuin varhaisin näytepiste, vaientaa molemmat
        # poikkeamasäännöt pysyvästi -- ja raportti väittäisi silloin "ei
        # poikkeamia" havaintona, vaikka yhtäkään näytepistettä ei koskaan
        # tutkittu. Kumpikaan osio ei voi tarkistaa tätä yksin, joten
        # tarkistus on täällä.
        earliest_sample = min(self.parse.snapshot_seconds)
        if self.thresholds.advance_max_sample_s < earliest_sample:
            raise ValueError(
                f"thresholds.advance_max_sample_s "
                f"({self.thresholds.advance_max_sample_s:g} s) on pienempi "
                f"kuin varhaisin parse.snapshot_seconds "
                f"({earliest_sample:g} s), joten yksikään näytepiste ei "
                "mahdu poikkeamasääntöjen aikarajaan.\n"
                "Kaikki kolme sääntöä vaikenisivat pysyvästi, ja raportin "
                "poikkeamaluku väittäisi 'ei poikkeamia' havaintona -- "
                "vaikka mitään ei tutkittu."
            )
        smallest_bonus = min(self.economy.loss_bonus_steps)
        if self.thresholds.normal_buy_money_min <= smallest_bonus:
            raise ValueError(
                f"thresholds.normal_buy_money_min "
                f"({self.thresholds.normal_buy_money_min}) on enintään pienin "
                f"häviöbonus ({smallest_bonus}), joten jokainen pelaaja "
                "läpäisisi ehdon B ilman senttiäkään omaa rahaa eikä yksikään "
                "hävityn jälkeinen ostos voisi enää olla force."
            )
        if self.thresholds.normal_buy_money_min > self.economy.max_money:
            raise ValueError(
                f"thresholds.normal_buy_money_min "
                f"({self.thresholds.normal_buy_money_min}) ylittää rahakaton "
                f"economy.max_money ({self.economy.max_money}), joten yksikään "
                "pelaaja ei voi koskaan täyttää ehtoa B eikä puoliostoa voi "
                "saavuttaa."
            )
        return self

    def require_faceit_api_key(self) -> str:
        """Palauta FACEIT Data API -avain tai kerro suomeksi, miten se asetetaan."""
        return self._require_secret(self.faceit_api_key, "FACEIT_API_KEY")

    def require_faceit_downloads_token(self) -> str:
        """Palauta FACEIT Downloads -token tai kerro suomeksi, miten se asetetaan."""
        return self._require_secret(
            self.faceit_downloads_token, "FACEIT_DOWNLOADS_TOKEN"
        )

    def _require_secret(self, value: SecretStr | None, name: str) -> str:
        if value is not None and value.get_secret_value().strip():
            return value.get_secret_value()
        path = self.secrets_file or secrets_env_path()
        raise SettingsError(
            f"Avainta {name} ei löytynyt.\n"
            f"Lisää tiedostoon {path} rivi:\n"
            f"    {name}=<oma avaimesi>\n"
            "Tiedosto on koneen oma eikä se ole OneDrivessa tai versionhallinnassa."
        )

    def secret_status(self, name: str) -> str:
        """Palauta avaimen tila sanana -- ``asetettu`` tai ``puuttuu``.

        Ei koskaan palauta itse avainta.
        """
        value = getattr(self, name.lower(), None)
        if isinstance(value, SecretStr) and value.get_secret_value().strip():
            return "asetettu"
        return "puuttuu"


def secrets_env_path() -> Path:
    """Koneen oma avaintiedosto ``%USERPROFILE%\\.pappascout\\.env``.

    Tarkoituksella OneDriven ulkopuolella: OneDrive tekisi tiedostosta
    konfliktikopioita kahdella koneella ja säilyttäisi kierrätetyn avaimen
    versiohistoriassa.
    """
    return Path.home() / ".pappascout" / ".env"


def project_env_path(start: Path | None = None) -> Path:
    """Projektin oma ``.env``, jota käytetään vain varalta."""
    return (start or Path.cwd()) / ".env"


def _repo_root() -> Path:
    """Repon juuri paketin sijainnista laskettuna (src-layout)."""
    return Path(__file__).resolve().parents[3]


def settings_search_paths(start: Path | None = None) -> list[Path]:
    """Polut, joista ``settings.toml`` etsitään, tärkeysjärjestyksessä."""
    paths: list[Path] = []
    from_env = os.environ.get(SETTINGS_ENV_VAR)
    if from_env:
        paths.append(Path(from_env))

    cwd = (start or Path.cwd()).resolve()
    for directory in [cwd, *cwd.parents]:
        paths.append(directory / SETTINGS_FILENAME)

    paths.append(_repo_root() / SETTINGS_FILENAME)

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def find_settings_file(start: Path | None = None) -> Path:
    """Etsi ``settings.toml`` tai kerro suomeksi, mistä sitä etsittiin.

    Jos ympäristömuuttuja on asetettu, se on käsky eikä ehdotus: puuttuva
    tiedosto on virhe, ei syy pudota takaisin työhakemistoon. Hiljainen
    varasija lukisi eri asetukset kuin käyttäjä pyysi.
    """
    from_env = os.environ.get(SETTINGS_ENV_VAR)
    if from_env:
        requested = Path(from_env)
        if not requested.is_file():
            raise SettingsError(
                f"Ympäristömuuttuja {SETTINGS_ENV_VAR} osoittaa tiedostoon "
                f"{requested}, jota ei ole.\n"
                "Korjaa polku tai poista muuttuja, jolloin settings.toml "
                "etsitään työhakemistosta."
            )
        return requested

    candidates = settings_search_paths(start)
    for path in candidates:
        if path.is_file():
            return path
    listing = "\n".join(f"    {path}" for path in candidates)
    raise SettingsError(
        "Asetustiedostoa settings.toml ei löytynyt.\n"
        "Etsin näistä poluista:\n"
        f"{listing}\n"
        "Siirry projektin juureen tai aseta ympäristömuuttuja "
        f"{SETTINGS_ENV_VAR} osoittamaan tiedostoon."
    )


def load_settings(
    settings_file: Path | None = None,
    env_files: tuple[Path, ...] | None = None,
) -> Settings:
    """Lataa asetukset TOML-tiedostosta ja avaimet ``.env``-tiedostoista.

    Args:
        settings_file: Asetustiedoston polku. Oletuksena etsitään
            :func:`settings_search_paths` -järjestyksessä.
        env_files: Avaintiedostot heikoimmasta vahvimpaan. Oletuksena projektin
            ``.env`` ensin ja koneen oma ``.env`` viimeisenä, jolloin koneen oma
            voittaa.

    Returns:
        Validoitu :class:`Settings`.

    Raises:
        SettingsError: Jos tiedostoa ei löydy, se ei ole kelvollista TOMLia tai
            jokin arvo ei kelpaa. Viesti kertoo aina, mitä pitää korjata.
    """
    path = Path(settings_file) if settings_file is not None else find_settings_file()
    if not path.is_file():
        raise SettingsError(
            f"Asetustiedostoa ei löytynyt polusta {path}.\n"
            "Luo tiedosto tai anna oikea polku."
        )

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(
            f"Asetustiedosto {path} ei ole kelvollista TOMLia: {exc}\n"
            "Korjaa syntaksi ja aja komento uudelleen."
        ) from exc
    except OSError as exc:
        raise SettingsError(
            f"Asetustiedostoa {path} ei voitu lukea: {exc}"
        ) from exc

    for (section, key), advice in REMOVED_SETTINGS.items():
        values = data.get(section)
        if isinstance(values, dict) and key in values:
            raise SettingsError(
                f"Asetustiedostossa {path} on avain [{section}].{key}, jota ei "
                "enää ole.\n"
                f"{advice}"
            )

    # Puuttuva osio ennen pydanticia. ``report: Field required`` on totta
    # muttei ohjaa mihinkään: se ei kerro, että osio on kokonaan pois, eikä
    # sitä mistä sen saa. Osion pakollisuus on linjaus eikä puute -- jokainen
    # asetus kirjoitetaan näkyviin, koska oletus, joka on vain koodissa, ei
    # näy tiedostossa jota säädetään -- joten korjattava on viesti.
    #
    # Sama sukulaisuus kuin :data:`REMOVED_SETTINGS`illa, joka kattaa
    # käänteisen tapauksen (asetus, jota ei enää ole). Kahden koneen arkisto
    # tekee kummastakin tavallisen tilanteen: repon ``settings.toml`` päivittyy
    # gitistä, ja väliin jäänyt pull näkyy juuri näin.
    missing = sorted(name for name in SETTINGS_SECTIONS if name not in data)
    if missing:
        raise SettingsError(
            f"Asetustiedostosta {path} puuttuu osio: "
            + ", ".join(f"[{name}]" for name in missing)
            + ".\n"
            "Jokainen osio on pakollinen, koska jokainen asetus kirjoitetaan "
            "näkyviin: koodin oletus ei näy tiedostossa, jota säädetään.\n"
            "Kopioi puuttuva osio repon omasta settings.tomlista -- sen saa "
            "näkyviin komennolla: git show HEAD:settings.toml"
        )

    unknown = sorted(set(data) - SETTINGS_SECTIONS)
    if unknown:
        raise SettingsError(
            f"Asetustiedostossa {path} on tuntematon osio tai avain: "
            f"{', '.join(unknown)}.\n"
            f"Sallitut osiot ovat {', '.join(sorted(SETTINGS_SECTIONS))}.\n"
            "Avaimia ei kirjoiteta tähän tiedostoon vaan tiedostoon "
            f"{secrets_env_path()}."
        )

    if env_files is None:
        env_files = (project_env_path(path.parent), secrets_env_path())
    # pydantic-settings: listan viimeinen tiedosto voittaa.
    existing = [str(p) for p in env_files if Path(p).is_file()]
    secrets_file = Path(existing[-1]) if existing else None

    try:
        return Settings(
            _env_file=existing or None,
            settings_file=path,
            secrets_file=secrets_file,
            **data,
        )
    except _ValidationError as exc:
        raise SettingsError(
            f"Asetustiedosto {path} ei kelpaa:\n"
            f"{_format_validation_error(exc)}\n"
            "Korjaa arvot ja aja komento uudelleen."
        ) from exc


def _format_validation_error(exc: _ValidationError) -> str:
    """Muotoile pydanticin virheet lyhyeksi suomenkieliseksi listaksi."""
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(juuri)"
        lines.append(f"    {location}: {error['msg']}")
    return "\n".join(lines)
