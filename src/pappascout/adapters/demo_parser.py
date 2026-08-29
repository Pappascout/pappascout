"""demoparser2-toteutus kierros-, näytepiste- ja tapahtumataululle (AD-8).

**Tämä on ainoa moduuli, jossa pelin propinimet esiintyvät.** Vaihe näkee vain
:class:`~pappascout.adapters.protocols.DemoParser`-portin, joten demoparser2:n
vaihtaminen tai päivittäminen ei kosketa putkea.

Kaikki alla käytetyt kentät on **todettu oikeasta demosta** (ks.
``_bmad-output/implementation-artifacts/demoparser2-kentat.md``), ei arvattu.

Kierrosrajat
------------
Kierros rajautuu kahden tapahtuman väliin:

``round_freeze_end``
    Ostoajan loppu. Tässä hetkessä luetaan **havaitut lähtöarvot**: raha,
    varustearvo freezetimen lopussa, kierroksen alun varustearvo ja puoli.
``round_end``
    Kierroksen ratkeaminen. Tässä hetkessä luetaan voittaja, voiton syy,
    eloonjääneet ja heidän varusteensa. Pelaajat eivät ole vielä syntyneet
    uudelleen -- ``round_officially_ended`` olisi liian myöhään, siinä
    kaikki kymmenen ovat jo elossa.

``round_end`` **on olemassa** demoparser2 0.42.0:ssa, vaikka se ei näy
``list_game_events()``-listalla. Se palauttaa sarakkeet ``round``, ``tick``,
``winner`` ja ``reason``, ja ensimmäinen rivi on tyhjä alkuarvo (tick 1).
``round`` on demon oma kierroslaskuri (Ancient 1..22, puukkokierros mukaan
lukien), ja se päätyy sellaisenaan ``round_raw``-sarakkeeseen.

Kierrosnumeroa **ei päätetä täällä**: adapteri palauttaa ``round_no``-sarakkeen
tyhjänä, ja ``stages.parse`` kutsuu ``domain.rounds.mark_played_rounds``ia.

Yksi kierrosraja ratkeaa silti jo täällä: **ottelun uudelleenaloitus**.
Liigademoissa puukkokierroksen jälkeen tulee oma ``round_freeze_end`` ilman
yhtään ``round_end``iä, ja peli jatkuu sen jälkeen normaalisti. Se ei ole
kierros, ja koska sillä ei ole demon omaa numeroa, se ei voi saada
``round_raw``:takaan -- ilman sitä ``stages.parse`` ei voisi tunnistaa sen
rivejä. Siksi se jää tässä numeroimattomaksi eikä tuota riviä yhteenkään
tauluun; lukumäärä kulkee diagnostiikassa ajon yhteenvetoon.

Tunnistus on **havaintoihin eikä sijaintiin** perustuva: uudelleenaloituksella
ei ole ``round_end``iä *ja* demon oma numerointi jatkuu sen yli yhdellä. Kumpi
tahansa ehto rikki keskeyttää parsinnan sen sijaan että kierros pudotettaisiin
hiljaa. Ks. :meth:`Demoparser2Adapter._assign_round_raw` ja
:meth:`Demoparser2Adapter._match_restarts`.

Uudelleenaloituksen aikana heitetty utility ei kuulu millekään kierrokselle:
sillä ei ole kierrosikkunaa, joten heitto päätyy lukuun
``grenades_outside_rounds`` -- samaan, jossa lämmittelyheitot ovat. Luku on
siis liigademossa normaalisti hieman suurempi kuin vanhassa demossa, eikä se
ole vika.

Pistemäärän mittauspisteet
--------------------------
``score_start`` luetaan kierroksen omasta freezetime-ankkurista ja ``score_end``
**seuraavan kierroksen** ankkurista. Syy on puukkokierros: sen tuottaman
pisteen näkee vielä sen omassa ``round_end``-tickissä, mutta ``mp_restartgame``
nollaa sen heti perään -- oman lopputickin lukema väittäisi puukkokierrosta
pelatuksi. Seuraavan ankkurin lukema on nollauksen jälkeinen ja siksi oikea.

Viimeisellä kierroksella seuraavaa ankkuria ei ole, joten sen ``score_end``
luetaan omasta ``round_end``-tickistä. Se on turvallista: pistemäärä on siinä
hetkessä jo kasvanut (todennettu molemmista testidemoista), eikä nollausta
enää tule. Sama varalähde on käytössä myös silloin, kun seuraavalta
kierrokselta puuttuu ankkuri.

Kenen arvot summataan
---------------------
Freezetimen lopun summat (raha, käytetty raha, varustearvo, kierroksen alun
varustearvo) lasketaan vain niistä pelaajista, joiden **kaikki** nämä propit
ovat luettavissa, ja ``players_freeze_end`` on saman joukon koko. Jakaja on
siis aina sama joukko kuin osoittaja: kolmen pelaajan summa viidellä jaettuna
näyttäisi ecolta, vaikka joukkue olisi ostanut täyden.

``players_armed_freeze_end`` lasketaan **samasta joukosta**: montako pelaajaa
oli aseistettu freezetimen lopussa. Summa ei kerro sitä -- kaksi AK:ta ja kolme
tyhjää antaa saman summan kuin viisi puolinaista.

Aseistettu = **panssari ja vähintään yksi ase hallussa**. Ase luetaan pelaajan
tavaraluettelosta (``inventory``) ja panssari propista ``m_ArmorValue``, eikä
varustearvosta: varustearvo on ase + panssari + kranaatit yhtenä lukuna, joten
Glock + kevlar + kaksi valoa (1250 $, mitattu Ancientista) näyttäisi
aseistetulta ilman yhtään asetta.

**Hallussapito, ei ostos.** Tavaraluettelo luetaan freezetimen lopusta, joten
edelliseltä kierrokselta säästetty tai vainajalta poimittu kivääri laskeutuu
samoin kuin juuri ostettu. Kierroksen kannalta ratkaisee mitä kädessä on, ei
mistä se tuli. Oletuspistoolit rajataan silti ulos: ne saa joka kierros
ilmaiseksi, joten niiden hallussapito ei kerro mitään.

**Lukukelvoton havainto tyhjentää koko rivin.** Jos yhdenkin luettavan pelaajan
panssari tai tavaraluettelo puuttuu, laskuri on ``null`` -- ei se luku, joka
saataisiin lopuista. Pelaaja pysyy ``players_freeze_end``in jakajassa, joten
hiljainen pudotus näyttäisi säästökierrokselta eikä lukuvirheeltä.

Luokittelu on **sallittujen aseiden luettelo** (:mod:`pappascout.constants`),
ei kiellettyjen: tuntematon nimi ei ole ase. Veitset ovat avoin joukko, jota
Valve kasvattaa, aseet suljettu. Tuntemattomat nimet kulkevat diagnostiikkaan
ja sieltä ajon yhteenvetoon -- hiljainen pudotus olisi yhtä paha kuin
hiljainen hyväksyntä.

Näytepisteet
------------
Sama lukukerta tuottaa myös ``ticks``-taulun: rivi per (pelaaja, kierros,
näytepiste). Näytepisteet valitsee :mod:`pappascout.domain.sampling`, joka on
puhdas funktio -- adapterin osuus on lukea propit valituilta tickeiltä ja
kertoa domainille, kummalla puolella kukin pelaaja on.

Kierrosrajat, kokoonpanot ja tickrate lasketaan **kerran** ja käytetään
molempiin tauluihin. Siksi portti palauttaa ne yhdessä
(:class:`~pappascout.adapters.protocols.DemoTables`): kaksi erillistä kutsua
tekisi kokoonpanojen tunnistuksen kahdesti, ja jos tulokset joskus eroaisivat,
``lineup_key`` olisi tauluissa eri eikä liitos enää osuisi.

Utility
-------
``grenade_thrown``-tapahtumaa **ei ole olemassa**, joten utility luetaan
``parse_grenades()``-lentoradoista: radan ensimmäinen piste on heitto ja
viimeinen räjähdys. Taulu on demon suurin yksittäinen erä -- Ancientissa
1 553 329 riviä -- ja se pelkistetään kahteen riviin per kranaatti heti
:func:`~pappascout.domain.utility.grenade_endpoints`illa, jolloin eteenpäin
kulkee noin 750 riviä.

Kaksi asiaa raakadatassa yllättää, ja molemmat on todettu Ancient-demolla:

* **Suurin osa riveistä ei ole lentorataa.** Kranaatti saa rivin myös pelaajan
  repussa ollessaan, ja silloin ``x, y, z`` ovat tyhjiä; 1,34 miljoonaa riviä
  1,55:stä on tällaisia. Lennossa tyyppi on ``...Projectile``, repussa ei.
* **``grenade_entity_id`` kierrätetään.** 374 lentorataa mahtuu 187
  tunnisteeseen. Jaksotus on siksi ``grenade_endpoints``in vastuulla, ei
  ryhmittelyn tunnisteen mukaan.

Lennossa molotov ja incendiary ovat molemmat ``CMolotovProjectile``. Erottelu
tehdään heittäjän repussa olevasta tyypistä heittoa edeltävällä tickillä
(``CMolotovGrenade`` / ``CIncendiaryGrenade``); jos se ei ratkea yksiselitteisesti,
tyypiksi jää ``molotov``.

Räjähdyksen paikka on ristiintarkistettu demon omiin tapahtumiin
(``smokegrenade_detonate``, ``hegrenade_detonate``, ``flashbang_detonate``):
radan viimeinen piste osuu niihin 0,024 pelin yksikön tarkkuudella kaikissa
281 tapauksessa. Tapahtumia ei silti lueta ajossa -- rata riittää, ja kolme
ylimääräistä tapahtumalukua maksaisi ilman lisätietoa.

Muistinkäyttö
-------------
Demoa ei ladata muistiin kokonaan. ``parse_ticks`` kutsutaan **vain
kierrosrajojen, näytepisteiden ja kranaattien päätepisteiden tickeille**
(Ancient: 44 + noin 100 + noin 750 tickiä), ei koko tickisarjalle. Kutsuja on
kolme eikä yksi, koska näytepisteiden tickit riippuvat tickratesta, joka
mitataan vasta kierrosrajojen lukemisesta, ja kranaattien tickit selviävät
vasta lentoradoista. Pakattu demo puretaan virtaavasti temp-tiedostoon.
"""

from __future__ import annotations

import hashlib
import statistics
import warnings
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from pappascout.adapters.decompress import readable_demo
from pappascout.adapters.protocols import (
    EVENTS_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
    ParseDiagnostics,
)
from pappascout.constants import ARMING_WEAPONS, KNOWN_INVENTORY_ITEMS
from pappascout.domain.sampling import (
    FIRST_CONTACT_SAMPLE,
    DamageEvent,
    RoundBounds,
    SamplePoint,
    first_contact_tick,
    sample_ticks,
    seconds_since_freeze_end,
)
from pappascout.domain.schemas import ARMED_COLUMN, EVENTS, ROUNDS, TICKS
from pappascout.domain.utility import (
    DETONATE,
    THROWN,
    PlayerPoint,
    flight_point,
    grenade_endpoints,
    snap_area,
    trajectory_gap_ticks,
)
from pappascout.errors import ParseError

__all__ = [
    "Demoparser2Adapter",
    "TEAM_SIDES",
    "TICK_PROPS",
    "SAMPLE_TICK_PROPS",
    "GRENADE_COLUMNS",
    "GRENADE_TYPES",
    "FIRE_ITEM_TYPES",
    "MOLOTOV_PROJECTILE",
    "DEFAULT_TICK_RATE",
    "TICK_RATE_MIN",
    "TICK_RATE_MAX",
    "MAX_MATCH_RESTARTS",
]

# -- Pelin kentät -------------------------------------------------------------

_TEAM_NUM = "CCSPlayerController.m_iTeamNum"
_ACCOUNT = "CCSPlayerController.CCSPlayerController_InGameMoneyServices.m_iAccount"
_CASH_SPENT = (
    "CCSPlayerController.CCSPlayerController_InGameMoneyServices"
    ".m_iCashSpentThisRound"
)
_EQUIP_FREEZE_END = "CCSPlayerPawn.m_unFreezetimeEndEquipmentValue"
_EQUIP_ROUND_START = "CCSPlayerPawn.m_unRoundStartEquipmentValue"
_EQUIP_CURRENT = "CCSPlayerPawn.m_unCurrentEquipmentValue"
_ARMOR_VALUE = "CCSPlayerPawn.m_ArmorValue"

#: Pelaajan tavaraluettelo: lista esineiden näyttönimiä (``AK-47``,
#: ``Smoke Grenade``, ``knife_t``, veitsiskinien omat nimet). Ei propinimi vaan
#: demoparser2:n oma johdettu sarake, ja ainoa lähde, josta näkee **mikä** ase
#: pelaajalla on -- varustearvo kertoo vain paljonko kalusto maksoi.
_INVENTORY = "inventory"

_LIFE_STATE = "CCSPlayerPawn.m_lifeState"
_TEAM_SCORE = "CCSTeam.m_iScore"
_ROUND_START_TIME = "CCSGameRulesProxy.CCSGameRules.m_fRoundStartTime"

#: Pelin oma aluenimi (``env_cs_place``). Noin kaksi kertaa karkeampi kuin
#: Total CS -callout; tyhjä merkkijono tarkoittaa aluetta, jolle peli ei anna
#: nimeä, ja se säilyy taulussa ``null``:na.
_PLACE_NAME = "CCSPlayerPawn.m_szLastPlaceName"

#: Pelaajan koordinaatit. demoparser2 palauttaa nämä valmiiksi float32:na.
_X = "X"
_Y = "Y"
_Z = "Z"

#: Propit, jotka luetaan kierrosrajojen tickeistä.
TICK_PROPS: tuple[str, ...] = (
    _TEAM_NUM,
    _ACCOUNT,
    _CASH_SPENT,
    _EQUIP_FREEZE_END,
    _EQUIP_ROUND_START,
    _EQUIP_CURRENT,
    _ARMOR_VALUE,
    _INVENTORY,
    _LIFE_STATE,
    _TEAM_SCORE,
    _ROUND_START_TIME,
)

#: Propit, jotka luetaan näytepisteiden tickeistä. Lyhyempi lista kuin
#: kierrosrajoilla: asetelmasta tarvitaan vain paikka, puoli ja elossaolo --
#: talousarvot ovat kierroksen ominaisuus, eivät hetken.
SAMPLE_TICK_PROPS: tuple[str, ...] = (
    _TEAM_NUM,
    _LIFE_STATE,
    _PLACE_NAME,
    _X,
    _Y,
    _Z,
)

#: Sarakkeet, jotka ``parse_grenades()``-taulussa on oltava. ``name`` on
#: mukana kirjastossa mutta jätetään lukematta: pelaajan nimi voi vaihtua
#: kesken ottelun, ja tunniste on ``steamid``.
GRENADE_COLUMNS: tuple[str, ...] = (
    "grenade_type",
    "grenade_entity_id",
    "x",
    "y",
    "z",
    "tick",
    "steamid",
)

#: Pelin luokkanimi lennossa -> kanoninen kranaattityyppi.
#:
#: Nämä ovat ``parse_grenades()``in ``grenade_type``-arvot niillä riveillä,
#: joilla on koordinaatit. Tuntematon nimi säilyy sellaisenaan: se on
#: harvinainen mutta luettava tulos, kun taas tyhjäksi muuttaminen hukkaisi
#: havainnon.
GRENADE_TYPES: dict[str, str] = {
    "CSmokeGrenadeProjectile": "smoke",
    "CFlashbangProjectile": "flashbang",
    "CHEGrenadeProjectile": "he",
    "CMolotovProjectile": "molotov",
    "CDecoyProjectile": "decoy",
}

#: Lennossa molotov ja incendiary ovat **sama** luokka.
MOLOTOV_PROJECTILE = "CMolotovProjectile"

#: Repussa ne erottuvat. Tästä kranaatin oikea tyyppi saadaan takaisin.
FIRE_ITEM_TYPES: dict[str, str] = {
    "CMolotovGrenade": "molotov",
    "CIncendiaryGrenade": "incendiary",
}

#: ``m_iTeamNum`` -> puoli. 0 ja 1 ovat katsoja ja liittymätön, eivät joukkueita.
TEAM_SIDES: dict[int, str] = {2: "T", 3: "CT"}

#: Sarakkeet, jotka ``player_hurt``- ja ``player_death``-tapahtumissa on oltava.
#: Molemmat tarjoavat kaikki neljä demoparser2 0.42.0:ssa.
DAMAGE_COLUMNS: tuple[str, ...] = (
    "tick",
    "attacker_steamid",
    "user_steamid",
    "weapon",
)

#: Elossa olevan pelaajan ``m_lifeState``. Muut arvot ovat kuollut tai kuolemassa.
_ALIVE = 0

#: CS2:n oletustickrate. Käytetään vain jos demosta ei saa mitattua arvoa.
DEFAULT_TICK_RATE = 64.0

#: Järkevyysrajat mitatulle tickratelle. CS2:n palvelimet ajavat 64 tai 128
#: tickiä; näiden ulkopuolinen arvo on mittausvirhe (esimerkiksi kellon nollaus
#: kesken ottelun), ei totuus.
TICK_RATE_MIN = 16.0
TICK_RATE_MAX = 256.0

#: Montako ottelun uudelleenaloitusta yhdessä demossa hyväksytään.
#:
#: Uudelleenaloitus on kierrosraja, jolla on freezetime-ankkuri mutta ei
#: ``round_end``iä, ja jonka **yli demon oma kierrosnumerointi jatkuu
#: yhdellä**. Liigaotteluissa niitä on tasan yksi, heti puukkokierroksen
#: jälkeen. Useampi tarkoittaisi ilmiötä, jota ei ole vielä nähty; silloin
#: parsinta pysähtyy eikä arvaa.
#:
#: Kaikki tästä johdetut viestit lukevat luvun täältä, jotta rajan nostaminen
#: ei jätä tekstejä valehtelemaan (ks.
#: :meth:`Demoparser2Adapter._match_restarts`).
MAX_MATCH_RESTARTS = 1


@dataclass
class _Lineup:
    """Yhden joukkueen kokoonpano yhdellä kartalla.

    ``members`` kasvaa kartan aikana, jos joukkue vaihtaa pelaajaa. Tunniste
    lasketaan kaikista kartalla pelanneista, jotta sama kokoonpano tuottaa
    saman avaimen ajosta toiseen.
    """

    members: set[str] = field(default_factory=set)

    def key(self) -> str:
        """Kokoonpanon tiiviste.

        Raises:
            ParseError: Jos kokoonpano on tyhjä. Tyhjän merkkijonon tiiviste
                olisi molemmilla joukkueilla sama, jolloin ``lineup_key`` ei
                erottaisi joukkueita lainkaan ja kaikki myöhempi ryhmittely
                menisi hiljaa väärin.
        """
        if not self.members:
            raise ParseError(
                "Demosta ei saatu tunnistettua kummankin joukkueen kokoonpanoa: "
                "toinen jäi tyhjäksi.\n"
                "Kierrosrajojen tickeistä ei löytynyt pelaajia molemmilta "
                "puolilta. Demo on todennäköisesti vioittunut tai katkennut."
            )
        raw = ",".join(sorted(self.members))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class _Segment:
    """Yksi kierrosraja demossa: pelattu kierros, ratkeamaton tai uudelleenaloitus.

    Attributes:
        demo_round: Demon oma kierrosnumero ``round_end``-tapahtuman
            ``round``-kentästä. ``None`` = segmentti ei ratkennut, tai
            demoparser2 ei antanut numeroa.
        freeze_end_tick: Kierroksen ankkuri, viimeinen ``round_freeze_end``
            ennen päättymistä. ``None`` = ankkuria ei ole, jolloin
            freezetimen lopun havaintoja ei voi lukea (``status`` kertoo sen).
        end_tick: Kierroksen ratkeamishetki ``round_end``-tapahtumasta.
            ``None`` = kierros ei ratkennut: demo katkesi kesken, tai
            segmentti ei ole kierros lainkaan.
        winner_side: Voittanut puoli (``"T"``/``"CT"``), tai ``None`` jos
            kierros ei ratkennut.
        win_reason: Voiton syy demon omalla nimellä, tai ``None`` samasta
            syystä kuin ``winner_side``.
        round_raw: Segmentille annettu demon oma kierrosnumero. ``None``
            tarkoittaa **ottelun uudelleenaloitusta**: se pelattiin, mutta se
            ei ole kierros eikä se tuota riviä yhteenkään tauluun -- samoin
            kuin puukkokierros. Ks.
            :meth:`Demoparser2Adapter._assign_round_raw`.
    """

    demo_round: int | None
    freeze_end_tick: int | None
    end_tick: int | None
    winner_side: str | None
    win_reason: str | None
    round_raw: int | None = None


@dataclass(frozen=True)
class _UtilityCounts:
    """Kranaatit, jotka eivät päätyneet tauluun sellaisenaan -- ja syy.

    Nämä eivät ole tauluun sopivia sarakkeita: pudotettu kranaatti ei voi olla
    rivi, eikä ratkeamaton tyyppi erotu ratkaistusta muuten kuin lukuna. Kaikki
    kulkevat siksi diagnostiikkaan ja sieltä ajon yhteenvetoon.

    Nolla on tavoitetila, mutta ei jokaiselle: ``outside_rounds`` on
    normaalisti 1-2, koska kierroksen ratkeamisen jälkeen heitetään yhä
    kranaatteja eikä niille ole ``t_s``:ää.

    Attributes:
        without_thrower: Rata ilman heittäjää.
        outside_rounds: Heitto, joka ei osu yhdenkään kierroksen rajoihin.
        unknown_side: Heittäjä, jonka puolta ei saatu selville.
        unknown_type: Kranaatti, jonka luokkanimeä ei tunneta. Nimi säilyy
            taulussa sellaisenaan, mutta luku paljastaa demoparser2:n
            uudelleennimeämisen ennen kuin se näkyy raportissa.
        fire_type_unresolved: Tulikranaatti, jonka molotov/incendiary-erottelu
            ei ratkennut. Tyypiksi jää ``molotov``, joten ilman lukua
            reppuhaun täydellinen rikkoutuminen näyttäisi täsmälleen samalta
            kuin demo, jossa heitettiin pelkkiä molotoveja.
        detonating_after_round: Räjähdys, joka osuu kierroksen päättymisen
            jälkeen. Rivi jää tauluun koordinaatteineen, mutta aluetta ei
            napsauteta: pelaajat ovat jo seuraavan kierroksen spawnissa.
        ticks_without_players: Päätepisteen tick, jolta ei saatu yhtään
            pelaajariviä. Toisin kuin muut tämän luokan luvut, tämä on **vika**
            eikä havainto -- se tarkoittaa, ettei aluetta voitu edes yrittää.
        id_reused_in_round: Kranaattipari, jonka tunniste toistuu saman
            kierroksen sisällä. Sopimus lupaa, että
            ``(round_no, grenade_entity_id)`` yksilöi parin.
    """

    without_thrower: int = 0
    outside_rounds: int = 0
    unknown_side: int = 0
    unknown_type: int = 0
    fire_type_unresolved: int = 0
    detonating_after_round: int = 0
    ticks_without_players: int = 0
    id_reused_in_round: int = 0


@dataclass
class _ArmedCounters:
    """Kalustolaskurin havainnot, jotka eivät mahdu ``ROUNDS``-sopimukseen.

    Attributes:
        unknown_items: Tavaraluettelon nimi -> montako kertaa se nähtiin.
            **Määrä eikä pelkkä joukko**: yksi eksoottinen veitsi ja
            demoparser2:n nimeämismuutos, joka osuu joka riviin, näyttäisivät
            pelkkänä nimenä täsmälleen samalta.
        unreadable_rows: Joukkuerivit, joilla laskuri jäi tyhjäksi siksi, että
            jonkun luettavan pelaajan panssari tai tavaraluettelo puuttui.
            Ankkurittomat kierrokset **eivät** ole tässä: niillä ei ole
            havaintoa lainkaan, mikä on eri asia kuin epäonnistunut luku.
    """

    unknown_items: Counter[str] = field(default_factory=Counter)
    unreadable_rows: int = 0


class Demoparser2Adapter:
    """Lukee kierros- ja näytepistetaulun demoparser2:lla.

    Toteuttaa :class:`~pappascout.adapters.protocols.DemoParser`-portin.

    Args:
        exclude_weapons: Aseet, jotka eivät kelpaa ensikontaktiksi
            (``[parse].first_contact_exclude_weapons``). Oletus on tarkoituksella
            tyhjä: adapteri ei lue asetuksia, vaan vaihe antaa listan.
        fallback_death: Saako ensikontakti tulla ``player_death``-tapahtumasta,
            jos kelvollista ``player_hurt``ia ei ole
            (``[parse].first_contact_fallback_death``).
        area_snap_units: Enimmäisetäisyys, jolta utility-tapahtuman alue saa
            napata lähimmän elossa olevan pelaajan alueen
            (``[parse].area_snap_units``). ``None`` = ei napsautusta, jolloin
            ``area`` jää tyhjäksi mutta koordinaatit tallentuvat.
    Aseistettujen laskurilla ei ole asetuksia: sääntö on "panssari ja
    vähintään yksi ase hallussa", ja aseluettelo on :mod:`pappascout.constants`.
    Luettelon muutos mitätöi arkiston ``stages.parse``in parametrihashin
    kautta, ei tämän luokan kautta.

    Attributes:
        diagnostics: Viimeisimmän parsinnan havainnot, jotka eivät mahdu
            taulusopimuksiin. ``None`` ennen ensimmäistä kutsua.
    """

    def __init__(
        self,
        *,
        exclude_weapons: Sequence[str] = (),
        fallback_death: bool = True,
        area_snap_units: float | None = None,
    ) -> None:
        self.exclude_weapons = tuple(exclude_weapons)
        self.fallback_death = fallback_death
        self.area_snap_units = area_snap_units
        self.diagnostics: ParseDiagnostics | None = None

    def parse_demo(
        self, path: Path, sample_seconds: Sequence[float]
    ) -> DemoTables:
        """Ks. portin dokumentaatio."""
        path = Path(path)
        with readable_demo(path) as demo_path:
            return self._parse(demo_path, path, tuple(sample_seconds))

    # -- Sisäinen ------------------------------------------------------------

    def _parse(
        self,
        demo_path: Path,
        original_path: Path,
        sample_seconds: tuple[float, ...],
    ) -> DemoTables:
        parser = self._open(demo_path, original_path)
        freeze_ticks = self._freeze_end_ticks(parser, original_path)
        round_ends = self._round_ends(parser, original_path)
        segments = self._segments(freeze_ticks, round_ends)

        if not segments:
            raise ParseError(
                f"Demosta {original_path.name} ei löytynyt yhtään kierrosta.\n"
                "Tiedosto on todennäköisesti katkennut kesken latauksen. "
                "Lataa demo uudelleen."
            )

        wanted = sorted(
            {s.freeze_end_tick for s in segments if s.freeze_end_tick is not None}
            | {s.end_tick for s in segments if s.end_tick is not None}
        )
        by_tick = self._read_ticks(parser, wanted, original_path)
        tick_rate, measured = self._tick_rate(by_tick, freeze_ticks)

        lineups = [_Lineup(), _Lineup()]
        sides = self._assign_sides(segments, by_tick, lineups)
        lineup_keys = self._lineup_keys(lineups)
        # Kalustolaskurin omat havainnot palautuvat taulun mukana eivätkä
        # kerry kutsujan antamaan olioon: muuttuva ulosparametri lakkaisi
        # hiljaa toimimasta, jos joku unohtaisi välittää sen eteenpäin.
        rounds, armed = self._build_frame(
            segments, by_tick, tick_rate, sides, lineup_keys
        )

        points, unknown_sides = self._sample_points(
            parser,
            original_path,
            segments,
            sides,
            lineups,
            by_tick,
            tick_rate,
            sample_seconds,
        )
        ticks, partial = self._build_ticks_frame(
            points, parser, original_path, segments, sides, lineup_keys
        )
        events, utility = self._build_events_frame(
            parser, original_path, segments, sides, lineup_keys, lineups, by_tick, tick_rate
        )

        self.diagnostics = ParseDiagnostics(
            tick_rate=tick_rate,
            tick_rate_measured=measured,
            rounds_seen=len(segments),
            match_restarts=sum(1 for s in segments if s.round_raw is None),
            partial_samples=partial,
            unknown_side_events=unknown_sides,
            grenades_without_thrower=utility.without_thrower,
            grenades_outside_rounds=utility.outside_rounds,
            grenades_unknown_side=utility.unknown_side,
            grenades_unknown_type=utility.unknown_type,
            grenades_fire_type_unresolved=utility.fire_type_unresolved,
            grenades_detonating_after_round=utility.detonating_after_round,
            grenade_ticks_without_players=utility.ticks_without_players,
            grenades_id_reused_in_round=utility.id_reused_in_round,
            unknown_inventory_items=tuple(sorted(armed.unknown_items.items())),
            armed_unreadable_rows=armed.unreadable_rows,
        )
        return DemoTables(rounds=rounds, ticks=ticks, events=events)

    def _open(self, demo_path: Path, original_path: Path) -> Any:
        from demoparser2 import DemoParser as _Demoparser2

        try:
            return _Demoparser2(str(demo_path))
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demoa {original_path.name} ei voitu avata: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut. Lataa demo uudelleen."
            ) from exc

    def _freeze_end_ticks(self, parser: Any, original_path: Path) -> list[int]:
        frame = self._event(parser, "round_freeze_end", original_path)
        if frame is None or "tick" not in frame.columns:
            return []
        return sorted({int(t) for t in frame["tick"].tolist()})

    def _round_ends(self, parser: Any, original_path: Path) -> list[dict[str, Any]]:
        """Kierrosten päättymiset aikajärjestyksessä.

        Ensimmäinen rivi (tick 1, ``round`` 0, tyhjä voittaja) on demoparser2:n
        alkuarvo eikä kierros, joten se pudotetaan.
        """
        frame = self._event(parser, "round_end", original_path)
        if frame is None or "tick" not in frame.columns:
            return []
        ends: list[dict[str, Any]] = []
        for row in frame.to_dict("records"):
            tick = _as_int(row.get("tick"))
            if tick is None or tick <= 1:
                continue
            ends.append(
                {
                    "tick": tick,
                    "round": _as_int(row.get("round")),
                    "winner": _as_side(row.get("winner")),
                    "reason": _as_str(row.get("reason")),
                }
            )
        ends.sort(key=lambda r: r["tick"])
        return ends

    def _event(self, parser: Any, name: str, original_path: Path) -> Any:
        try:
            frame = parser.parse_event(name)
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} tapahtumaa {name!r} ei voitu lukea: "
                f"{exc}\n"
                "Tiedosto on todennäköisesti katkennut. Lataa demo uudelleen."
            ) from exc
        if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
            return None
        return frame

    @staticmethod
    def _segments(
        freeze_ticks: list[int], round_ends: list[dict[str, Any]]
    ) -> list[_Segment]:
        """Paritä freezetime-ankkurit ja kierrosten päättymiset.

        Kierroksen ankkuri on viimeinen ``round_freeze_end`` ennen sen
        päättymistä. Jos ankkuria ei ole, kierros on silti mukana --
        ``freeze_end_tick`` jää tyhjäksi ja ``status`` kertoo syyn (AD-9).
        Jos ankkurin jälkeen ei tule päättymistä, demo on katkennut kesken
        kierroksen; kierros pysyy mukana, mutta ilman tulosta.
        """
        segments: list[_Segment] = []
        pending: list[int] = []
        i = 0
        for end in round_ends:
            while i < len(freeze_ticks) and freeze_ticks[i] < end["tick"]:
                pending.append(freeze_ticks[i])
                i += 1
            # Kaikki paitsi viimeinen ankkuri jäivät ilman päättymistä.
            for orphan in pending[:-1]:
                segments.append(_Segment(None, orphan, None, None, None))
            segments.append(
                _Segment(
                    demo_round=end["round"],
                    freeze_end_tick=pending[-1] if pending else None,
                    end_tick=end["tick"],
                    winner_side=end["winner"],
                    win_reason=end["reason"],
                )
            )
            pending = []
        for orphan in freeze_ticks[i:]:
            segments.append(_Segment(None, orphan, None, None, None))

        Demoparser2Adapter._assign_round_raw(segments)
        return segments

    @staticmethod
    def _assign_round_raw(segments: list[_Segment]) -> None:
        """Anna jokaiselle kierrokselle demon oma juokseva numero.

        Arvo tulee ``round_end``-tapahtuman ``round``-kentästä. Segmentti, joka
        jää ilman omaa arvoa, käsitellään sen mukaan **mistä kohtaa listaa se
        löytyy ja mitä siitä on havaittu**:

        * **Listan hännässä** se on ratkeamaton kierros: demo katkesi kesken,
          eikä ``round_end``iä enää tule. Sille johdetaan naapureista arvo,
          joka säilyttää järjestyksen.
        * **Listan alussa**, ennen demon ensimmäistä omaa numeroa, se saa
          arvonsa taaksepäin laskettuna. Numerointi voi alkaa mistä tahansa,
          eikä sitä ennen ole arvoa, johon törmätä.
        * **Keskellä** se on ottelun uudelleenaloitus -- mutta vain jos
          havainnot sanovat niin. Sen ratkaisee
          :meth:`_match_restarts`, joka keskeyttää parsinnan jos ehdot eivät
          täyty. Uudelleenaloitus jää **numeroimattomaksi**
          (``round_raw = None``) samalla mekanismilla kuin puukkokierros:
          naapurista täyttäminen antaisi sille numeron, jonka demo käyttää
          heti perään uudelleen.

        Raises:
            ParseError: Jos keskellä oleva numeroimaton kierrosraja ei täytä
                uudelleenaloituksen ehtoja, jos niitä on enemmän kuin
                :data:`MAX_MATCH_RESTARTS`, tai jos numerointi ei kasva
                tasaisesti.
        """
        if not segments:
            return
        raws: list[int | None] = [s.demo_round for s in segments]
        own = [index for index, value in enumerate(raws) if value is not None]

        if not own:
            # Yhdelläkään segmentillä ei ole demon omaa numeroa. Järjestys on
            # silti tiedossa, joten varasääntö on juokseva numerointi.
            raws = list(range(1, len(raws) + 1))
        else:
            first_own, last_own = own[0], own[-1]

            # Häntä: näiden jälkeen ei tule enää yhtään demon omaa numeroa,
            # joten ne ovat ratkeamattomia kierroksia.
            value = raws[last_own]
            assert value is not None
            for index in range(last_own + 1, len(raws)):
                value += 1
                raws[index] = value

            # Alku: ennen ensimmäistä omaa numeroa ei ole arvoa, johon törmätä.
            value = raws[first_own]
            assert value is not None
            for index in range(first_own - 1, -1, -1):
                value -= 1
                raws[index] = value

            # Keskelle jääneet tarkistetaan havaintoja vasten; hyväksytyt
            # jäävät None:ksi eli numeroimattomiksi.
            Demoparser2Adapter._match_restarts(segments, raws, own)

        known = [value for value in raws if value is not None]
        for first, second in zip(known, known[1:]):
            if second <= first:
                raise ParseError(
                    "Demon oma kierrosnumerointi ei kasva tasaisesti "
                    f"({first} -> {second}).\n"
                    "Kierrosrajat eivät vastaa demoparser2:n round_end-numeroita, "
                    "joten kierroksia ei voi tunnistaa luotettavasti."
                )

        for segment, number in zip(segments, raws):
            segment.round_raw = number

    @staticmethod
    def _match_restarts(
        segments: list[_Segment], raws: list[int | None], own: list[int]
    ) -> list[int]:
        """Keskellä olevat kierrosrajat, jotka ovat ottelun uudelleenaloituksia.

        Tunnistus perustuu **havaintoihin eikä sijaintiin**. Pelkkä "keskellä
        ja ilman numeroa" ei riitä: samalta näyttäisi myös kierros, jonka
        rajojen tunnistus hukkasi, ja sen pudottaminen veisi kierroksen pois
        jokaisesta taulusta ja nimeäisi sen vielä uudelleenaloitukseksi.

        Uudelleenaloitus täyttää molemmat ehdot:

        * **Ei ``round_end``iä.** Segmentti, joka ratkesi mutta jolta puuttuu
          demon oma numero, on kierros ilman numeroa -- ei uudelleenaloitus.
          Se numeroidaan naapurista kuten ennenkin: kierros on olemassa, joten
          sitä ei pudoteta. Jos johdettu numero törmää demon omaan,
          monotonisuustarkistus hoitaa sen.
        * **Demon oma numerointi jatkuu sen yli yhdellä.** Uudelleenaloitus ei
          kuluta kierrosnumeroa, joten sen molemmin puolin numerot ovat
          peräkkäiset. Hyppy tarkoittaa, että väliin on jäänyt kierros; se
          keskeyttää parsinnan, koska pudotus siirtäisi kaiken jälkeen tulevan.

        Args:
            segments: Kierrosrajat aikajärjestyksessä.
            raws: Kullekin segmentille päätetty numero; ``None`` niillä, joita
                ei ole numeroitu. **Muutetaan paikallaan**: aukot, jotka eivät
                ole uudelleenaloituksia, täytetään tässä.
            own: Niiden segmenttien indeksit, joilla on demon oma numero.

        Returns:
            Uudelleenaloitusten indeksit ``segments``-listassa. Ne jäävät
            ``raws``issa ``None``:ksi.

        Raises:
            ParseError: Jos demon numerointi hyppää numeroimattoman
                kierrosrajan yli, tai jos uudelleenaloituksia on enemmän kuin
                :data:`MAX_MATCH_RESTARTS`.
        """
        restarts: list[int] = []
        for previous, following in zip(own, own[1:]):
            gap = list(range(previous + 1, following))
            if not gap:
                continue

            if any(segments[i].end_tick is not None for i in gap):
                # Väliin jäi kierros, joka ratkesi mutta jolta puuttuu demon
                # oma numero. Se on kierros eikä uudelleenaloitus, joten se
                # numeroidaan naapurista -- pudottaminen veisi sen pois
                # jokaisesta taulusta ja nimeäisi sen vielä väärin.
                value = raws[previous]
                assert value is not None
                for index in gap:
                    value += 1
                    raws[index] = value
                continue

            before, after = raws[previous], raws[following]
            assert before is not None and after is not None
            if after != before + 1:
                ticks = ", ".join(str(segments[i].freeze_end_tick) for i in gap)
                raise ParseError(
                    "Demon oma kierrosnumerointi hyppää numeroimattoman "
                    f"kierrosrajan yli ({before} -> {after}, freezetime-tickit "
                    f"{ticks}).\n"
                    "Uudelleenaloituksen yli numerointi jatkuisi yhdellä, joten "
                    "väliin on jäänyt kierros, jota kierrosrajojen tunnistus ei "
                    "löytänyt. Sitä ei pudoteta arvaamalla: katso demoa "
                    "listatuista tickeistä ja kerro havainto kehittäjälle."
                )

            restarts.extend(gap)

        if len(restarts) > MAX_MATCH_RESTARTS:
            ticks = ", ".join(str(segments[i].freeze_end_tick) for i in restarts)
            raise ParseError(
                f"Demossa on {len(restarts)} ottelun uudelleenaloitukselta "
                f"näyttävää kierrosrajaa (freezetime-tickit {ticks}), mutta "
                f"enintään {MAX_MATCH_RESTARTS} hyväksytään.\n"
                "Useampi tarkoittaa ilmiötä, jota ei ole vielä nähty, eikä sitä "
                "arvata. Avaa demo listatuista tickeistä ja kerro havainto "
                "kehittäjälle ennen kuin tulosta käytetään."
            )
        return restarts

    def _read_ticks(
        self, parser: Any, ticks: list[int], original_path: Path
    ) -> dict[int, list[dict[str, Any]]]:
        """Lue propit annetuista tickeistä ja ryhmittele tickin mukaan."""
        if not ticks:
            return {}
        try:
            frame = parser.parse_ticks(list(TICK_PROPS), ticks=ticks)
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} tick-arvoja ei voitu lukea: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut tai demoparser2:n "
                "versio ei tunne näitä kenttiä. Aja: uv sync"
            ) from exc

        received = set(getattr(frame, "columns", ()))
        missing = [
            name for name in (*TICK_PROPS, "tick", "steamid") if name not in received
        ]
        if missing:
            raise ParseError(
                "demoparser2 ei palauttanut kaikkia pyydettyjä kenttiä demosta "
                f"{original_path.name}. Puuttuu: {', '.join(missing)}.\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Ilman tarkistusta taulu näyttäisi kelvolliselta "
                "mutta olisi tyhjä. Päivitä adapters/demo_parser.py:n propinimet."
            )

        by_tick: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in frame.to_dict("records"):
            steamid = _as_str(row.get("steamid"))
            side = TEAM_SIDES.get(_as_int(row.get(_TEAM_NUM)) or -1)
            tick = _as_int(row.get("tick"))
            if steamid is None or side is None or tick is None:
                # Katsojat ja liittymättömät eivät ole kierroksen osapuolia.
                continue
            by_tick[tick].append(
                {
                    "steamid": steamid,
                    "side": side,
                    "account": _as_int(row.get(_ACCOUNT)),
                    "cash_spent": _as_int(row.get(_CASH_SPENT)),
                    "equip_freeze_end": _as_int(row.get(_EQUIP_FREEZE_END)),
                    "equip_round_start": _as_int(row.get(_EQUIP_ROUND_START)),
                    "equip_current": _as_int(row.get(_EQUIP_CURRENT)),
                    "armor_value": _as_int(row.get(_ARMOR_VALUE)),
                    "inventory": _as_inventory(row.get(_INVENTORY)),
                    "alive": _as_int(row.get(_LIFE_STATE)) == _ALIVE,
                    "team_score": _as_int(row.get(_TEAM_SCORE)),
                    "round_start_time": _as_float(row.get(_ROUND_START_TIME)),
                }
            )
        return dict(by_tick)

    @staticmethod
    def _tick_rate(
        by_tick: dict[int, list[dict[str, Any]]], freeze_ticks: list[int]
    ) -> tuple[float, bool]:
        """Laske tickrate kierrosten alkuaikojen ja tickien suhteesta.

        ``m_fRoundStartTime`` on pelin sekuntikello, tick demon oma laskuri.
        Kahden kierroksen välinen suhde antaa tickraten suoraan; mediaani
        suojaa yksittäiseltä uudelleenkäynnistykseltä. Demon otsikossa
        tickratea ei ole.

        Returns:
            ``(tickrate, mitattiinko)``. Jos mittausta ei saatu tai tulos on
            järkevyysrajojen ulkopuolella, palautetaan
            :data:`DEFAULT_TICK_RATE` ja ``False`` -- vaihe kertoo sen
            käyttäjälle, jottei oletus mene läpi mittauksena.
        """
        observations: list[float] = []
        previous: tuple[int, float] | None = None
        for tick in freeze_ticks:
            rows = by_tick.get(tick) or []
            time_s = next(
                (
                    r["round_start_time"]
                    for r in rows
                    if r["round_start_time"] is not None
                ),
                None,
            )
            if time_s is None:
                continue
            if previous is not None:
                d_tick = tick - previous[0]
                d_time = time_s - previous[1]
                if d_tick > 0 and d_time > 0:
                    observations.append(d_tick / d_time)
            previous = (tick, time_s)
        if not observations:
            return DEFAULT_TICK_RATE, False
        rate = statistics.median(observations)
        if not TICK_RATE_MIN <= rate <= TICK_RATE_MAX:
            return DEFAULT_TICK_RATE, False
        rounded = round(rate)
        clean = float(rounded) if abs(rate - rounded) < 0.05 else float(rate)
        return clean, True

    @staticmethod
    def _lineup_keys(lineups: list[_Lineup]) -> list[str]:
        """Kokoonpanojen tunnisteet; ne eivät saa olla samat.

        Sama tunniste tarkoittaisi, ettei joukkueita voi erottaa toisistaan --
        ja silloin jokainen joukkuekohtainen luku olisi molempien summa.
        """
        lineup_keys = [lineup.key() for lineup in lineups]
        if lineup_keys[0] == lineup_keys[1]:
            raise ParseError(
                "Molemmille joukkueille tuli sama kokoonpanotunniste, joten "
                "niitä ei voi erottaa toisistaan.\n"
                "Kierrosrajojen tickeissä näkyy sama pelaajajoukko molemmilla "
                "puolilla. Demo on todennäköisesti vioittunut."
            )
        return lineup_keys

    def _build_frame(
        self,
        segments: list[_Segment],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
    ) -> tuple[pl.DataFrame, _ArmedCounters]:
        armed = _ArmedCounters()
        anchor_score = [
            _total_score(by_tick.get(s.freeze_end_tick or -1) or []) for s in segments
        ]
        end_score = [_total_score(by_tick.get(s.end_tick or -1) or []) for s in segments]

        # Edellisen kierroksen eloonjääneiden varustearvo, joukkueittain.
        previous_saved: list[int | None] = [None, None]
        rows: list[dict[str, Any]] = []

        for index, segment in enumerate(segments):
            freeze_rows = by_tick.get(segment.freeze_end_tick or -1) or []
            end_rows = by_tick.get(segment.end_tick or -1) or []

            # Tuntemattomat nimet skannataan **kaikilta** ankkurin riveiltä,
            # ei vain laskuriin kelpaavilta: uusi asenimi voi esiintyä
            # ensimmäisen kerran pelaajalla, jonka talousarvot eivät ole
            # luettavissa (_readable pudottaa hänet), ja silloin se jäisi
            # raportoimatta juuri siitä demosta, joka sen toi.
            #
            # Jäljelle jäävä rajaus: kierros ilman freezetime-ankkuria ei
            # tuota yhtään riviä, joten sen nimiä ei nähdä lainkaan. Nimi
            # esiintyy silloin lähes varmasti myös jollain toisella
            # kierroksella, joten se ei ole hiljainen aukko vaan viive.
            for row in freeze_rows:
                for name in row.get("inventory") or ():
                    if name not in KNOWN_INVENTORY_ITEMS:
                        armed.unknown_items[name] += 1

            # Numeroimaton segmentti (ottelun uudelleenaloitus) ei ole
            # kierros: se ei tuota riviä. Ankkurin tavaraluettelot luetaan
            # silti yllä, koska uusi asenimi voi esiintyä ensimmäisen kerran
            # juuri siinä. Segmentti pysyy listassa, jotta edellisen
            # kierroksen ``score_end`` luetaan yhä **sen** ankkurista --
            # juuri siitä lukemasta puukkokierroksen nollaus näkyy.
            if segment.round_raw is None:
                # Uudelleenaloitus nollaa kaluston, joten seuraava kierros ei
                # peri eloonjääneiden varusteita sitä edeltäneeltä
                # kierrokselta. Sama tulos kuin ennenkin: haamulla ei ole
                # päättymistickiä, joten sen oma summa olisi tyhjä.
                previous_saved = [None, None]
                continue

            score_start = anchor_score[index]
            if score_start is None:
                score_start = _score_before(index, segments, anchor_score, end_score)
            if score_start is None:
                score_start = end_score[index]

            score_end = anchor_score[index + 1] if index + 1 < len(segments) else None
            if score_end is None:
                score_end = end_score[index]

            saved_now: list[int | None] = [None, None]
            for team_index, side in enumerate(sides[index]):
                own_freeze = _readable(
                    [r for r in freeze_rows if r["side"] == side]
                )
                own_end = [r for r in end_rows if r["side"] == side]
                alive = [r for r in own_end if r["alive"]]
                armed_count = _armed_count(own_freeze)
                # Tyhjä joukko on ankkuriton kierros, ei lukuvirhe -- vain
                # jälkimmäinen lasketaan, jotta luku kertoo propivikaa eikä
                # normaalia puutetta.
                if armed_count is None and own_freeze:
                    armed.unreadable_rows += 1
                saved_now[team_index] = (
                    _sum_or_zero([r["equip_current"] for r in alive])
                    if own_end
                    else None
                )
                rows.append(
                    {
                        "round_raw": segment.round_raw,
                        "round_no": None,
                        "lineup_key": lineup_keys[team_index],
                        "side": side,
                        "won": (
                            None
                            if segment.winner_side is None
                            else segment.winner_side == side
                        ),
                        "win_reason": segment.win_reason,
                        "money_freeze_end": _sum_or_none(
                            [r["account"] for r in own_freeze]
                        ),
                        "money_spent": _sum_or_none(
                            [r["cash_spent"] for r in own_freeze]
                        ),
                        "equip_freeze_end": _sum_or_none(
                            [r["equip_freeze_end"] for r in own_freeze]
                        ),
                        "equip_round_start": _sum_or_none(
                            [r["equip_round_start"] for r in own_freeze]
                        ),
                        # Kynnykset ovat per pelaaja, joten jakaja on
                        # havaittava eikä oletettava: vajaalla pelaava
                        # joukkue näyttäisi viidellä jaettuna ecolta.
                        # Jakaja on sama joukko kuin summissa (ks. _readable).
                        "players_freeze_end": len(own_freeze) or None,
                        # Sama joukko kuin summissa ja jakajassa. Kaksi eri
                        # jakajaa samalla rivillä olisi vika, joka näkyisi
                        # vasta raportissa.
                        ARMED_COLUMN: armed_count,
                        "survivors": len(alive) if own_end else None,
                        "survivors_equip_prev": previous_saved[team_index],
                        "freeze_end_tick": segment.freeze_end_tick,
                        "tick_rate": tick_rate,
                        "status": (
                            "ok"
                            if segment.freeze_end_tick is not None
                            else "no_freeze_end"
                        ),
                        "score_start": score_start,
                        "score_end": score_end,
                    }
                )
            previous_saved = saved_now

        return self._typed_frame(rows), armed

    @staticmethod
    def _assign_sides(
        segments: list[_Segment],
        by_tick: dict[int, list[dict[str, Any]]],
        lineups: list[_Lineup],
    ) -> list[tuple[str, str]]:
        """Päätä kummalla puolella kumpikin kokoonpano on kullakin kierroksella.

        Joukkueet vaihtavat puolta puoliajalla ja jatkoajassa, joten puoli ei
        kelpaa joukkueen tunnisteeksi. Kokoonpanot tunnistetaan pelaajajoukkojen
        päällekkäisyydestä: se kestää sekä puolenvaihdon että yksittäisen
        pelaajavaihdon.

        Tasapeliä **ei ratkaista arvaamalla**. Jos kumpikaan kuvaus ei voita,
        käytetään edellisen kierroksen kuvausta; jos edellistäkään ei ole,
        parsinta keskeytetään. Hiljainen oletus kohdistaisi voitot väärälle
        joukkueelle.

        **Ottelun uudelleenaloitus ohitetaan kokonaan.** Se on juuri se hetki,
        jolloin joukkue- ja puolitila on epävakain: pelaajia siirretään,
        yhdistetään uudelleen ja puolet asetetaan uusiksi. Yksikin väärä lukema
        siellä jäisi pysyvästi ``lineups``iin ja voisi kääntää puolet kaikille
        sen jälkeisille kierroksille. Segmentti saa silti oman alkionsa, jotta
        lista pysyy segmenttien mittaisena; sitä ei käytetä mihinkään, koska
        uudelleenaloitus ei tuota riviä yhteenkään tauluun.

        Returns:
            Kierroksittain pari ``(kokoonpanon 0 puoli, kokoonpanon 1 puoli)``.
        """
        result: list[tuple[str, str]] = []
        previous: tuple[str, str] | None = None

        for segment in segments:
            if segment.round_raw is None:
                result.append(
                    _require_previous(previous, segment, "ottelun uudelleenaloitus")
                )
                continue
            rows = (
                by_tick.get(segment.freeze_end_tick or -1)
                or by_tick.get(segment.end_tick or -1)
                or []
            )
            sets_by_side = {
                side: {r["steamid"] for r in rows if r["side"] == side}
                for side in ("T", "CT")
            }
            if not sets_by_side["T"] and not sets_by_side["CT"]:
                result.append(_require_previous(previous, segment, "ei pelaajia"))
                continue

            if not lineups[0].members and not lineups[1].members:
                if not sets_by_side["T"] or not sets_by_side["CT"]:
                    raise ParseError(
                        "Ensimmäiseltä tunnistetulta kierrokselta löytyi "
                        "pelaajia vain toiselta puolelta, joten kokoonpanoja ei "
                        "voi erottaa.\n"
                        "Demo on todennäköisesti katkennut alusta."
                    )
                lineups[0].members |= sets_by_side["T"]
                lineups[1].members |= sets_by_side["CT"]
                previous = ("T", "CT")
                result.append(previous)
                continue

            direct = sum(
                len(sets_by_side[side] & lineups[i].members)
                for i, side in enumerate(("T", "CT"))
            )
            swapped = sum(
                len(sets_by_side[side] & lineups[i].members)
                for i, side in enumerate(("CT", "T"))
            )
            if direct == swapped:
                sides = _require_previous(
                    previous, segment, "kokoonpanot eivät erotu toisistaan"
                )
            else:
                sides = ("T", "CT") if direct > swapped else ("CT", "T")
            for i, side in enumerate(sides):
                lineups[i].members |= sets_by_side[side]
            previous = sides
            result.append(sides)
        return result

    @staticmethod
    def _typed_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Rakenna taulu sopimuksen tyypeillä.

        Tyypit annetaan eksplisiittisesti, koska pelkistä null-arvoista Polars
        päättelisi ``Null``-tyypin ja ``schemas.validate`` hylkäisi taulun.
        ``score_start`` ja ``score_end`` ovat osa porttisopimusta
        (``ROUNDS_ADAPTER_COLUMNS``); ``stages.parse`` pudottaa ne ennen
        kirjoitusta.
        """
        schema: dict[str, Any] = {
            name: ROUNDS.get(name, pl.Int32) for name in ROUNDS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema, orient="row")

    # -- Näytepisteet --------------------------------------------------------

    def _sample_points(
        self,
        parser: Any,
        original_path: Path,
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineups: list[_Lineup],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        sample_seconds: tuple[float, ...],
    ) -> tuple[list[SamplePoint], int]:
        """Valitse hetket, joilta pelaajien sijainnit luetaan.

        Aikapisteet tulevat suoraan :func:`~pappascout.domain.sampling.sample_ticks`
        -funktiolta. Ensikontakti ratkaistaan kierros kerrallaan, koska sen
        sääntö vaatii tiedon siitä, kummalla puolella kumpikin pelaaja oli
        **tällä** kierroksella -- puolet vaihtuvat puoliajalla.

        Returns:
            ``(näytepisteet, tuntemattoman puolen takia ohitetut tapahtumat)``.
        """
        # Ottelun uudelleenaloitusta ei näytteistetä: sillä ei ole
        # kierrosnumeroa, johon rivit kiinnittyisivät. Alkuperäinen indeksi
        # kulkee mukana, koska ``sides`` ja ``segments`` ovat segmenttien
        # järjestyksessä -- ilman sitä kaikki uudelleenaloituksen jälkeiset
        # kierrokset lukisivat edellisen segmentin puolet.
        sampled: list[tuple[int, _Segment]] = []
        bounds: list[RoundBounds] = []
        for index, segment in enumerate(segments):
            raw = segment.round_raw
            if raw is None:
                continue
            sampled.append((index, segment))
            bounds.append(
                RoundBounds(
                    round_raw=raw,
                    freeze_end_tick=segment.freeze_end_tick,
                    end_tick=segment.end_tick,
                )
            )
        points = sample_ticks(bounds, tick_rate, sample_seconds)

        hurt = self._damage_events(parser, "player_hurt", original_path)
        deaths = (
            self._damage_events(parser, "player_death", original_path)
            if self.fallback_death
            else []
        )
        if not hurt and not deaths:
            return _sorted_points(points), 0

        lineup_of = _lineup_index_by_player(lineups)
        unknown_sides = 0
        for position, round_bounds in enumerate(bounds):
            if not round_bounds.is_samplable:
                continue
            index = sampled[position][0]
            player_sides = _side_lookup(lineup_of, sides[index], segments[index], by_tick)
            own_hurt, a = _with_sides(hurt, round_bounds, player_sides)
            own_deaths, b = _with_sides(deaths, round_bounds, player_sides)
            unknown_sides += a + b
            tick = first_contact_tick(
                own_hurt,
                round_bounds,
                exclude_weapons=self.exclude_weapons,
                death_events=own_deaths,
                fallback_death=self.fallback_death,
            )
            if tick is None:
                continue
            assert round_bounds.freeze_end_tick is not None  # is_samplable
            t_s = seconds_since_freeze_end(tick, round_bounds.freeze_end_tick, tick_rate)
            points.append(
                SamplePoint(
                    round_raw=round_bounds.round_raw,
                    tick=tick,
                    sample_kind=FIRST_CONTACT_SAMPLE,
                    sample_t_s=t_s,
                    t_s=t_s,
                )
            )
        return _sorted_points(points), unknown_sides

    def _damage_events(
        self, parser: Any, name: str, original_path: Path
    ) -> list[tuple[int, str | None, str | None, str | None]]:
        """Lue ``player_hurt``- tai ``player_death``-tapahtumat.

        Puolia ei liitetä tässä: sama pelaaja on eri puolella ennen ja jälkeen
        puoliajan, joten kuvaus on kierroskohtainen.

        Returns:
            ``(tick, attacker_id, victim_id, weapon)``. Puuttuva tapahtuma ei
            ole virhe -- kierros voi ratketa ilman yhtään vahinkoa.
        """
        frame = self._event(parser, name, original_path)
        if frame is None:
            # Tapahtumaa ei ole demossa lainkaan. Se on mahdollista (kierros
            # voi ratketa ilman vahinkoa), joten se ei ole virhe.
            return []

        missing = [
            column for column in DAMAGE_COLUMNS if column not in frame.columns
        ]
        if missing:
            raise ParseError(
                f"Demon {original_path.name} tapahtumasta {name!r} puuttuu "
                f"sarake: {', '.join(missing)}.\n"
                "Ilman sitä jokainen tapahtuma hylättäisiin äänettömästi ja "
                "tulos väittäisi, ettei yhdelläkään kierroksella ollut "
                "ensikontaktia. Kenttä on todennäköisesti nimetty uudelleen "
                "demoparser2:n päivityksessä -- päivitä "
                "adapters/demo_parser.py:n DAMAGE_COLUMNS."
            )

        rows: list[tuple[int, str | None, str | None, str | None]] = []
        for row in frame.to_dict("records"):
            tick = _as_int(row.get("tick"))
            if tick is None:
                continue
            rows.append(
                (
                    tick,
                    _as_str(row.get("attacker_steamid")),
                    _as_str(row.get("user_steamid")),
                    _as_str(row.get("weapon")),
                )
            )
        return rows

    def _build_ticks_frame(
        self,
        points: list[SamplePoint],
        parser: Any,
        original_path: Path,
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
    ) -> tuple[pl.DataFrame, int]:
        """Lue pelaajien sijainnit näytepisteiden tickeiltä ja rakenna taulu.

        Rivi syntyy **jokaisesta** pelaajasta, myös kuolleesta: kuolleiden
        suodatus on aggregoinnin työ (AD-10), ei parsinnan. Tuntematon alue
        jää ``null``:ksi, mutta koordinaatit tallentuvat silti -- riviä ei
        pudoteta hiljaa.

        Returns:
            ``(taulu, vajaiden näytepisteiden määrä)``. Vajaa näytepiste on
            sellainen, jolta saatiin vähemmän pelaajia kuin demon parhaalta
            pisteeltä. Luku raportoidaan, koska systemaattinen propivika
            näkyisi muuten vasta vinoutuneina aggregaatteina.
        """
        if not points:
            return self._typed_ticks_frame([]), 0

        wanted = sorted({p.tick for p in points})
        by_tick = self._read_sample_ticks(parser, wanted, original_path)
        # sides on segmenttien järjestyksessä, mutta näytepiste tuntee vain
        # round_raw-arvon, joten kuvaus tarvitaan takaisin segmentti-indeksiin.
        index_by_raw = {
            s.round_raw: index
            for index, s in enumerate(segments)
            if s.round_raw is not None
        }
        # Laiskasti eikä ahnaasti: _keys_by_side nostaa ParseErrorin, jos
        # molemmille kokoonpanoille tuli sama puoli. Ahne rakennus antaisi
        # uudelleenaloitukselle vallan kaataa koko ajon, vaikka se ei tuota
        # riviä yhteenkään tauluun -- ja virheviesti kertoisi sen round_raw:ksi
        # ``None``, joka ei auta lukijaa mihinkään.
        keys_per_round: dict[int, dict[str, str]] = {}

        rows: list[dict[str, Any]] = []
        players_per_point: list[int] = []
        for point in points:
            segment_index = index_by_raw.get(point.round_raw)
            if segment_index is None:  # pragma: no cover - sample_ticks takaa
                continue
            side_keys = keys_per_round.get(segment_index)
            if side_keys is None:
                side_keys = _keys_by_side(
                    sides[segment_index], lineup_keys, segments[segment_index]
                )
                keys_per_round[segment_index] = side_keys
            tick_rows = by_tick.get(point.tick, ())
            if not tick_rows:
                raise ParseError(
                    f"Demon {original_path.name} naytepisteeltä "
                    f"(round_raw={point.round_raw}, {point.sample_kind}, "
                    f"t={point.sample_t_s:g} s, tick={point.tick}) ei saatu "
                    "yhtään pelaajariviä.\n"
                    "Tick on kierroksen rajojen sisällä, joten tyhjä tulos "
                    "tarkoittaa että demo on vioittunut tai demoparser2 ei "
                    "palauta tältä tickiltä mitään. Näytepiste laskettaisiin "
                    "mukaan lukuihin mutta puuttuisi taulusta."
                )
            players_per_point.append(len(tick_rows))
            for row in tick_rows:
                side = row["side"]
                rows.append(
                    {
                        "round_raw": point.round_raw,
                        "round_no": None,
                        "player_id": row["steamid"],
                        "lineup_key": side_keys[side],
                        "side": side,
                        "sample_kind": point.sample_kind,
                        "sample_t_s": point.sample_t_s,
                        "t_s": point.t_s,
                        "x": row["x"],
                        "y": row["y"],
                        "z": row["z"],
                        "area": row["area"],
                        "is_alive": row["alive"],
                    }
                )

        # Odotettu pelaajamäärä luetaan demosta itsestään: [thresholds] ei näy
        # tähän vaiheeseen (AD-3), joten roster_size'a ei voi käyttää.
        full_count = max(players_per_point, default=0)
        partial = sum(1 for count in players_per_point if count < full_count)
        return self._typed_ticks_frame(rows), partial

    def _read_sample_ticks(
        self, parser: Any, ticks: list[int], original_path: Path
    ) -> dict[int, list[dict[str, Any]]]:
        """Lue sijaintipropit annetuilta tickeiltä ja ryhmittele tickin mukaan."""
        if not ticks:
            return {}
        try:
            frame = parser.parse_ticks(list(SAMPLE_TICK_PROPS), ticks=ticks)
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} näytepisteitä ei voitu lukea: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut tai demoparser2:n "
                "versio ei tunne näitä kenttiä. Aja: uv sync"
            ) from exc

        received = set(getattr(frame, "columns", ()))
        missing = [
            name
            for name in (*SAMPLE_TICK_PROPS, "tick", "steamid")
            if name not in received
        ]
        if missing:
            raise ParseError(
                "demoparser2 ei palauttanut kaikkia näytepisteen kenttiä "
                f"demosta {original_path.name}. Puuttuu: {', '.join(missing)}.\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Ilman tarkistusta asetelmataulu näyttäisi "
                "kelvolliselta mutta olisi tyhjä tai paikaton. Päivitä "
                "adapters/demo_parser.py:n propinimet."
            )

        by_tick: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in frame.to_dict("records"):
            steamid = _as_str(row.get("steamid"))
            side = TEAM_SIDES.get(_as_int(row.get(_TEAM_NUM)) or -1)
            tick = _as_int(row.get("tick"))
            if steamid is None or side is None or tick is None:
                # Katsojat ja liittymättömät eivät ole kierroksen osapuolia.
                continue
            life_state = _as_int(row.get(_LIFE_STATE))
            if life_state is None:
                # is_alive ei ole nullable, joten puuttuva arvo muuttuisi
                # hiljaa arvoksi False ja elossa oleva pelaaja katoaisi
                # aggregoinnista. Tuntematon alue saa jäädä nulliksi, mutta
                # tämä ei voi.
                raise ParseError(
                    f"Demon {original_path.name} tickistä {tick} puuttuu "
                    f"pelaajan {steamid} {_LIFE_STATE}.\n"
                    "Elossaolo on pakollinen havainto: puuttuvasta arvosta "
                    "tulisi 'kuollut', ja pelaaja katoaisi asetelmasta "
                    "äänettömästi. Tarkista demoparser2:n versio."
                )
            by_tick[tick].append(
                {
                    "steamid": steamid,
                    "side": side,
                    # Tyhjä aluenimi on pelin tapa sanoa "ei nimettyä aluetta".
                    # Se säilyy null:na; koordinaatit kertovat silti paikan.
                    "area": _as_str(row.get(_PLACE_NAME)),
                    "x": _as_float(row.get(_X)),
                    "y": _as_float(row.get(_Y)),
                    "z": _as_float(row.get(_Z)),
                    "alive": life_state == _ALIVE,
                }
            )
        return dict(by_tick)

    @staticmethod
    def _typed_ticks_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Rakenna näytepistetaulu sopimuksen tyypeillä.

        Tyypit annetaan eksplisiittisesti samasta syystä kuin kierrostaulussa:
        pelkistä null-arvoista Polars päättelisi ``Null``-tyypin.
        """
        schema: dict[str, Any] = {name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS}
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema, orient="row")

    # -- Utility -------------------------------------------------------------

    def _build_events_frame(
        self,
        parser: Any,
        original_path: Path,
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
        lineups: list[_Lineup],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
    ) -> tuple[pl.DataFrame, _UtilityCounts]:
        """Lue lentoradat ja rakenna niistä ``EVENTS``-muotoinen taulu.

        Järjestys on tarkoituksellinen: rata pelkistetään päätepisteiksi
        **ennen** kuin mitään muuta tehdään, jolloin 1,55 miljoonaa riviä
        kutistuu noin 750:een eikä kulje vaiheiden läpi kokonaisena.

        Kierros ratkeaa **heitosta**: kierroksen lopussa heitetty savu kuuluu
        sille kierrokselle, jolta se lähti, vaikka se palaisi vasta seuraavan
        puolella. Molemmat rivit saavat siis saman ``round_raw``:n, ja
        räjähdyksen ``t_s`` voi ylittää kierroksen keston -- se on oikea
        havainto eikä virhe.

        Alue on kahdenlaista tietoa. Heittäjällä on oma ``m_szLastPlaceName``
        samalta tickiltä, joten heiton alue on **havainto**
        (``area_source = "observed"``). Kranaatilla ei ole aluenimeä, joten
        räjähdyksen alue on lähimmältä elossa olevalta pelaajalta johdettu
        **approksimaatio** (``"snapped"``), ja sen etäisyys tallentuu, jotta
        kuluttaja voi erottaa varman osuman kaukaisesta arviosta.

        Returns:
            ``(taulu, luvut)``. Taulu on tyhjä mutta sopimuksen mukainen, jos
            demossa ei ollut yhtään heitettyä kranaattia.
        """
        raw = self._read_grenades(parser, original_path)
        if raw.is_empty():
            return self._typed_events_frame([]), _UtilityCounts()

        endpoints, without_thrower = self._endpoints(raw, tick_rate, original_path)
        if endpoints.is_empty():
            return (
                self._typed_events_frame([]),
                _UtilityCounts(without_thrower=without_thrower),
            )
        unknown_type = _unknown_type_count(endpoints)
        endpoints, fire_unresolved = _name_fire_grenades(
            endpoints, raw, trajectory_gap_ticks(tick_rate)
        )

        windows = _round_windows(segments)
        starts = [window[0] for window in windows]

        round_of_throw: dict[int, int] = {}
        outside = 0
        throws = endpoints.filter(pl.col("event_kind") == THROWN)
        for row in throws.iter_rows(named=True):
            index = _round_of_tick(starts, windows, row["tick"])
            if index is None:
                outside += 1
                continue
            round_of_throw[row["grenade_no"]] = index

        lineup_of = _lineup_index_by_player(lineups)
        sides_by_round: dict[int, dict[str, str]] = {}
        keys_by_round: dict[int, dict[str, str]] = {}

        selected: list[dict[str, Any]] = []
        unknown_side_count = 0
        for row in endpoints.iter_rows(named=True):
            index = round_of_throw.get(row["grenade_no"])
            if index is None:
                continue
            if index not in sides_by_round:
                sides_by_round[index] = _side_lookup(
                    lineup_of, sides[index], segments[index], by_tick
                )
                keys_by_round[index] = _keys_by_side(
                    sides[index], lineup_keys, segments[index]
                )
            side = sides_by_round[index].get(row["thrower_id"])
            if side is None:
                # Kranaatti pudotetaan kokonaan, mutta lasketaan kerran --
                # heitosta, jotta luku on kranaatteja eikä rivejä.
                if row["event_kind"] == THROWN:
                    unknown_side_count += 1
                continue
            selected.append(
                {
                    **row,
                    "_segment": index,
                    "_side": side,
                    "_lineup": keys_by_round[index][side],
                }
            )

        wanted = sorted({r["tick"] for r in selected})
        # Tyhjä lista **ei** saa mennä parse_ticksille: se voisi tarkoittaa
        # "kaikki tickit", eli juuri sen koko tickisarjan luvun, jonka tämä
        # moduuli lupaa välttää. Tilanne syntyy, jos jokainen kranaatti putoaa
        # kierrosten ulkopuolisena tai tuntemattoman puolen takia.
        positions = (
            self._read_sample_ticks(parser, wanted, original_path) if wanted else {}
        )
        empty_ticks = sum(1 for tick in wanted if not positions.get(tick))

        rows: list[dict[str, Any]] = []
        late_detonations = 0
        for r in selected:
            segment = segments[r["_segment"]]
            freeze_end = segment.freeze_end_tick
            end_tick = segment.end_tick
            if freeze_end is None or end_tick is None:
                # _round_windows rakennetaan vain ankkurillisista kierroksista,
                # joten tämä ei voi tapahtua. Tarkistus on silti oikea eikä
                # assert: assert katoaa python -O:lla, ja seurauksena olisi
                # TypeError kesken 233 MB:n demon parsinnan.
                raise ParseError(
                    f"Demon {original_path.name} kranaatti kohdistui kierrokselle "
                    f"(round_raw={segment.round_raw}), jolta puuttuu ankkuri tai "
                    "päättymistick.\n"
                    "Ilman niitä t_s:ää ei voi laskea. Demo on todennäköisesti "
                    "vioittunut."
                )
            tick_players = positions.get(r["tick"], ())
            if r["event_kind"] == DETONATE and r["tick"] > end_tick:
                late_detonations += 1
            area, source, distance = self._resolve_area(r, end_tick, tick_players)
            rows.append(
                {
                    "round_raw": segment.round_raw,
                    "round_no": None,
                    "event_kind": r["event_kind"],
                    "grenade_entity_id": r["grenade_entity_id"],
                    "grenade_type": r["grenade_type"],
                    "thrower_id": r["thrower_id"],
                    "lineup_key": r["_lineup"],
                    "side": r["_side"],
                    "t_s": seconds_since_freeze_end(r["tick"], freeze_end, tick_rate),
                    "x": r["x"],
                    "y": r["y"],
                    "z": r["z"],
                    "area": area,
                    "area_source": source,
                    "snap_distance": distance,
                }
            )

        frame = self._typed_events_frame(rows)
        counts = _UtilityCounts(
            without_thrower=without_thrower,
            outside_rounds=outside,
            unknown_side=unknown_side_count,
            unknown_type=unknown_type,
            fire_type_unresolved=fire_unresolved,
            detonating_after_round=late_detonations,
            ticks_without_players=empty_ticks,
            id_reused_in_round=_id_reuse_count(frame),
        )
        return frame, counts

    def _resolve_area(
        self,
        row: dict[str, Any],
        end_tick: int,
        tick_players: Sequence[dict[str, Any]],
    ) -> tuple[str | None, str | None, float | None]:
        """Päätä rivin alue, sen lähde ja mahdollinen napsautusetäisyys.

        Heitolle alue on **havainto**: heittäjä itse on paikalla ja hänen
        ``m_szLastPlaceName``insä on luettavissa samalta tickiltä. Napsautus
        voisi tarttua vieressä seisovaan kaveriin, vaikka oikea vastaus on
        tiedossa.

        Räjähdykselle alue on approksimaatio -- paitsi jos rata jatkuu
        kierroksen päättymisen yli. Silloin tickin pelaajat ovat jo seuraavan
        kierroksen spawnissa, ja napsautus kertoisi missä joukkue on **nyt**
        eikä missä savu on. Alue jätetään silloin tyhjäksi ja tapaus lasketaan.
        """
        if row["event_kind"] == THROWN:
            for player in tick_players:
                if player["steamid"] == row["thrower_id"]:
                    area = player["area"]
                    return area, ("observed" if area is not None else None), None
            return None, None, None

        if row["tick"] > end_tick:
            return None, None, None

        snap = snap_area(
            row["x"],
            row["y"],
            row["z"],
            [
                PlayerPoint(
                    x=p["x"], y=p["y"], z=p["z"], area=p["area"], is_alive=p["alive"]
                )
                for p in tick_players
            ],
            self.area_snap_units,
        )
        source = "snapped" if snap.area is not None else None
        return snap.area, source, snap.distance

    def _endpoints(
        self, raw: pl.DataFrame, tick_rate: float, original_path: Path
    ) -> tuple[pl.DataFrame, int]:
        """Kutsu domainin pelkistystä ja käännä sen virheet suomeksi.

        Puuttuva sarake voi paljastua kahdessa kohdassa: Polars nostaa
        ``ColumnNotFoundError``in jo muunnoksessa, ja ``grenade_endpoints``
        nostaa ``ValueError``in omassa tarkistuksessaan. Kumpi tahansa on sama
        vika kuin :meth:`_read_grenades`in oma tarkistus havaitsee, joten
        kaikkien kolmen on näytettävä käyttäjälle samalta -- eikä paljaalta
        pinojäljeltä.
        """
        try:
            return grenade_endpoints(
                _trajectory_frame(raw),
                max_gap_ticks=trajectory_gap_ticks(tick_rate),
            )
        except (ValueError, pl.exceptions.PolarsError) as exc:
            raise ParseError(
                f"Demon {original_path.name} lentoratoja ei voitu pelkistää: "
                f"{exc}\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Päivitä adapters/demo_parser.py:n "
                "GRENADE_COLUMNS."
            ) from exc

    def _read_grenades(self, parser: Any, original_path: Path) -> pl.DataFrame:
        """Lue ``parse_grenades()`` ja tarkista, että sarakkeet ovat tallella.

        Tyhjä tulos ei ole virhe: demossa ei välttämättä heitetty yhtään
        kranaattia. Puuttuva **sarake** on virhe, koska silloin tulos olisi
        tyhjä eikä sitä voisi erottaa utilityttömästä demosta.
        """
        try:
            frame = parser.parse_grenades()
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {original_path.name} lentoratoja ei voitu lukea: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut tai demoparser2:n "
                "versio ei tunne parse_grenades-metodia. Aja: uv sync"
            ) from exc

        if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
            return pl.DataFrame()

        missing = [name for name in GRENADE_COLUMNS if name not in frame.columns]
        if missing:
            raise ParseError(
                "demoparser2 ei palauttanut kaikkia lentoradan kenttiä demosta "
                f"{original_path.name}. Puuttuu: {', '.join(missing)}.\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Ilman tarkistusta utility-taulu olisi tyhjä ja "
                "näyttäisi demolta, jossa ei heitetty yhtään kranaattia. "
                "Päivitä adapters/demo_parser.py:n GRENADE_COLUMNS."
            )
        return _as_polars(frame, GRENADE_COLUMNS)

    @staticmethod
    def _typed_events_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Rakenna tapahtumataulu sopimuksen tyypeillä ja vakaassa järjestyksessä.

        Lajittelu on eksplisiittinen: matkan varrella tehdyt liitokset eivät
        säilytä rivijärjestystä, ja sama demo tuottaisi muuten eri tavut eri
        ajoilla. ``event_kind`` on Enum, joten sen järjestys on luettelon
        järjestys -- heitto ennen räjähdystä.
        """
        schema: dict[str, Any] = {
            name: EVENTS[name] for name in EVENTS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema, orient="row").sort(
            "round_raw", "grenade_entity_id", "event_kind", "t_s"
        )


def _as_polars(frame: Any, columns: Sequence[str]) -> pl.DataFrame:
    """Muunna demoparser2:n taulu Polarsiksi, vain pyydetyt sarakkeet.

    Sarakevalinta tehdään **ennen** muunnosta: ``name`` on 1,55 miljoonan rivin
    merkkijonosarake, jota ei tarvita mihinkään -- tunniste on ``steamid``.
    """
    if isinstance(frame, pl.DataFrame):
        return frame.select(columns)
    return pl.from_pandas(frame[list(columns)])


def _thrower_id() -> pl.Expr:
    """``steamid`` merkkijonoksi niin, ettei tunniste mene liukuluvuksi.

    Pandas nostaa kokonaislukusarakkeen ``float64``:ksi heti kun siinä on yksi
    tyhjä arvo. Suora ``cast(Utf8)`` tekisi silloin jokaisesta tunnisteesta
    muotoa ``"7.6561e+16"``, puolihaku ei osuisi yhteenkään pelaajaan ja
    **kaikki kranaatit putoaisivat tuntemattomana puolena** -- taulu olisi
    tyhjä eikä mikään kertoisi miksi. Kierto kokonaisluvun kautta antaa saman
    desimaalimuodon kuin tickien ``steamid``.
    """
    return pl.coalesce(
        pl.col("steamid").cast(pl.Int64, strict=False).cast(pl.Utf8),
        pl.col("steamid").cast(pl.Utf8, strict=False),
    )


def _trajectory_frame(raw: pl.DataFrame) -> pl.DataFrame:
    """Lentorata domainin sarakenimillä ja tyypeillä."""
    return raw.select(
        pl.col("grenade_entity_id").cast(pl.Int32),
        pl.col("grenade_type").cast(pl.Utf8),
        _thrower_id().alias("thrower_id"),
        pl.col("tick").cast(pl.Int32),
        pl.col("x").cast(pl.Float32),
        pl.col("y").cast(pl.Float32),
        pl.col("z").cast(pl.Float32),
    )


def _unknown_type_count(endpoints: pl.DataFrame) -> int:
    """Kranaatit, joiden luokkanimeä ei tunneta.

    Tuntematon nimi säilyy taulussa sellaisenaan -- se on luettava havainto --
    mutta demoparser2:n uudelleennimeäminen vuotaisi muuten tauluun ilman
    varoitusta, ja raportti näyttäisi utilityä, jonka tyyppi on pelin
    C++-luokan nimi.
    """
    return int(
        endpoints.filter(
            (pl.col("event_kind") == THROWN)
            & ~pl.col("grenade_type").is_in(list(GRENADE_TYPES))
        ).height
    )


def _name_fire_grenades(
    endpoints: pl.DataFrame, raw: pl.DataFrame, tolerance: int
) -> tuple[pl.DataFrame, int]:
    """Käännä luokkanimet kanonisiksi ja erota molotov incendiarystä.

    Lennossa molemmat ovat ``CMolotovProjectile``, joten erottelu on haettava
    heittäjän repusta heittoa edeltävältä hetkeltä: siellä kranaatti on yhä
    ``CMolotovGrenade`` tai ``CIncendiaryGrenade``. Haku on ``join_asof`` eikä
    tarkka tick: lentoradalle sallitaan pieni aukko, ja repulle on sallittava
    sama -- yksi hukkuva tick ei saa muuttaa incendiarya molotoviksi.

    Molemmat tulikranaatit repussa (poimittu vastustajan pudottama) jättää
    tyypin ratkaisematta; arvaus antaisi puolet ajasta väärän vastauksen ja
    näyttäisi silti havainnolta.

    Returns:
        ``(taulu, ratkeamattomat)``. Jälkimmäinen kattaa sekä osumattomat että
        epäselvät. Ilman lukua reppuhaun **täydellinen** epäonnistuminen --
        luokkanimen muutos, liian tiukka toleranssi -- näyttäisi täsmälleen
        samalta kuin demo, jossa heitettiin pelkkiä molotoveja.
    """
    canonical = endpoints.with_columns(
        pl.col("grenade_type").replace(GRENADE_TYPES)
    )
    fire_throws = (
        endpoints.filter(
            (pl.col("event_kind") == THROWN)
            & (pl.col("grenade_type") == MOLOTOV_PROJECTILE)
        )
        .select("grenade_no", "thrower_id", pl.col("tick").alias("throw_tick"))
        .sort("throw_tick")
    )
    if fire_throws.is_empty():
        return canonical, 0

    in_inventory = raw.filter(~flight_point()).select(
        _thrower_id().alias("thrower_id"),
        pl.col("tick").cast(pl.Int32),
        pl.col("grenade_type").cast(pl.Utf8),
    )

    # Yksi asof-liitos per tyyppi: se kertoo, kumpia tulikranaatteja heittäjällä
    # oli repussa juuri ennen heittoa. Kaksi osumaa on epäselvä tapaus, yksi
    # ratkaisee tyypin, nolla jättää sen auki.
    names = list(FIRE_ITEM_TYPES.values())
    matches = fire_throws.select("grenade_no")
    for class_name, name in FIRE_ITEM_TYPES.items():
        own = (
            in_inventory.filter(pl.col("grenade_type") == class_name)
            .select("thrower_id", "tick")
            .unique()
            .sort("tick")
        )
        if own.is_empty():
            matches = matches.with_columns(pl.lit(False).alias(name))
            continue
        with warnings.catch_warnings():
            # Polars ei voi tarkistaa lajittelua, kun ryhmittely on annettu, ja
            # varoittaa siitä joka kutsulla. Molemmat kehykset on lajiteltu
            # tickin mukaan tässä funktiossa, joten varoitus olisi pelkkää
            # kohinaa käyttäjän ruudulla kesken parsinnan.
            warnings.simplefilter("ignore", UserWarning)
            joined = fire_throws.join_asof(
                own,
                left_on="throw_tick",
                right_on="tick",
                by="thrower_id",
                strategy="backward",
                tolerance=tolerance,
            ).select("grenade_no", pl.col("tick").is_not_null().alias(name))
        matches = matches.join(joined, on="grenade_no", how="left")

    resolved = matches.with_columns(
        pl.sum_horizontal(
            [pl.col(name).fill_null(False).cast(pl.Int8) for name in names]
        ).alias("_osumia")
    )
    unresolved = int(resolved.filter(pl.col("_osumia") != 1).height)

    unambiguous = resolved.filter(pl.col("_osumia") == 1).select(
        "grenade_no",
        pl.coalesce(
            [
                pl.when(pl.col(name).fill_null(False)).then(
                    pl.lit(name, dtype=pl.Utf8)
                )
                for name in names
            ]
        ).alias("fire_type"),
    )
    if unambiguous.is_empty():
        return canonical, unresolved

    renamed = (
        canonical.join(unambiguous, on="grenade_no", how="left")
        .with_columns(
            pl.when(pl.col("fire_type").is_not_null())
            .then(pl.col("fire_type"))
            .otherwise(pl.col("grenade_type"))
            .alias("grenade_type")
        )
        .drop("fire_type")
        .sort("grenade_no", "tick")
    )
    return renamed, unresolved


def _id_reuse_count(frame: pl.DataFrame) -> int:
    """Kranaatit, joiden tunniste toistuu **saman kierroksen sisällä**.

    ``(round_no, grenade_entity_id)`` on luvattu parin avaimeksi kaikelle
    myöhemmälle työlle. Peli kierrättää tunnisteet demon aikana, mutta ei
    havaintojen mukaan kierroksen sisällä. Jos niin joskus kävisi, avain
    lakkaisi yksilöimästä paria ja aggregointi laskisi kaksi savua yhdeksi --
    joten tapaus lasketaan ja kerrotaan sen sijaan, että se paljastuisi vasta
    raportin luvuista.
    """
    if frame.is_empty():
        return 0
    return int(
        frame.group_by("round_raw", "grenade_entity_id", "event_kind")
        .len()
        .filter(pl.col("len") > 1)
        .height
    )


def _round_windows(segments: list[_Segment]) -> list[tuple[int, int, int]]:
    """Kierrosten ``[ankkuri, loppu]``-ikkunat aikajärjestyksessä.

    Raises:
        ParseError: Jos ikkunat menevät päällekkäin. Silloin
            :func:`_round_of_tick`in binäärihaku voisi kohdistaa kranaatin
            väärälle kierrokselle -- ja kierroksen jokainen utility-havainto
            olisi väärän joukkueen suunnitelmaa.
    """
    windows = sorted(
        (s.freeze_end_tick, s.end_tick, index)
        for index, s in enumerate(segments)
        if s.freeze_end_tick is not None and s.end_tick is not None
    )
    for first, second in zip(windows, windows[1:]):
        if second[0] <= first[1]:
            raise ParseError(
                "Demon kierrosrajat menevät päällekkäin: kierros alkaa tickistä "
                f"{second[0]} vaikka edellinen päättyy vasta tickissä {first[1]}.\n"
                "Kranaattia ei voi silloin kohdistaa yksikäsitteisesti "
                "kierrokselle. Demo on todennäköisesti vioittunut."
            )
    return windows


def _round_of_tick(
    starts: list[int], windows: list[tuple[int, int, int]], tick: int
) -> int | None:
    """Kierros, jonka rajojen sisään tick osuu, tai ``None``.

    Ikkunat eivät mene päällekkäin (:func:`_round_windows` varmistaa sen),
    joten viimeinen ankkuri ennen tickiä on ainoa ehdokas. ``None`` tarkoittaa
    lämmittelyä ennen ensimmäistä ankkuria tai heittoa kierroksen ratkeamisen
    ja seuraavan ostoajan välissä; kummallakaan ``t_s`` ei ole määritelty.
    """
    position = bisect_right(starts, tick) - 1
    if position < 0:
        return None
    _, end, index = windows[position]
    return index if tick <= end else None


def _keys_by_side(
    sides: tuple[str, str], lineup_keys: list[str], segment: _Segment
) -> dict[str, str]:
    """Puoli -> kokoonpanotunniste yhdellä kierroksella.

    Sanakirja eikä ``sides.index(side)``: jos puolikuvaus olisi jostain syystä
    ``("T", "T")``, ``.index`` palauttaisi molemmille nollan ja **molemmat
    joukkueet saisivat saman lineup_keyn**. Taulu näyttäisi kelvolliselta,
    mutta jokainen joukkuekohtainen luku olisi molempien summa -- täsmälleen se
    ristiinkytkentä, jonka :meth:`Demoparser2Adapter._lineup_keys` estää
    kierrostaulussa.
    """
    if sides[0] == sides[1]:
        raise ParseError(
            f"Kierroksella (round_raw={segment.round_raw}, "
            f"freeze_end_tick={segment.freeze_end_tick}) molemmille "
            f"kokoonpanoille tuli sama puoli {sides[0]!r}.\n"
            "Puolet eivät erotu, joten näytepisteiden rivit kohdistuisivat "
            "samalle joukkueelle. Demo on todennäköisesti vioittunut."
        )
    return {sides[0]: lineup_keys[0], sides[1]: lineup_keys[1]}


def _lineup_index_by_player(lineups: list[_Lineup]) -> dict[str, int]:
    """Pelaaja -> kokoonpanon indeksi.

    Pelaaja, joka on ehtinyt näkyä molemmissa kokoonpanoissa, jätetään pois:
    hänen puoltaan ei voi päätellä, ja arvaus kohdistaisi kontaktin väärin
    päin. Sellaista ei normaalissa demossa esiinny.
    """
    result: dict[str, int] = {}
    in_both = lineups[0].members & lineups[1].members
    for index, lineup in enumerate(lineups):
        for steamid in lineup.members - in_both:
            result[steamid] = index
    return result


def _side_lookup(
    lineup_of: dict[str, int],
    sides: tuple[str, str],
    segment: _Segment,
    by_tick: dict[int, list[dict[str, Any]]],
) -> dict[str, str]:
    """Pelaaja -> puoli **tällä kierroksella**.

    Ensisijainen lähde on kokoonpano: puoli tulee kierroksen omasta
    kuvauksesta, ei pelaajasta, koska joukkueet vaihtavat puolta puoliajalla ja
    jatkoajassa.

    Varalähteenä on kierroksen oman tickin ``m_iTeamNum``. Sitä tarvitaan
    pelaajalle, joka ei ole kummassakaan kokoonpanossa -- kesken karttaa tullut
    tai uudelleenyhdistänyt pelaaja. Ilman varalähdetta hanen vahinkonsa
    hylättäisiin äänettömästi ja kierros voisi menettaa ensikontaktinsa.
    """
    player_sides = {steamid: sides[index] for steamid, index in lineup_of.items()}
    for tick in (segment.freeze_end_tick, segment.end_tick):
        for row in by_tick.get(tick or -1) or ():
            player_sides.setdefault(row["steamid"], row["side"])
    return player_sides


def _with_sides(
    events: list[tuple[int, str | None, str | None, str | None]],
    bounds: RoundBounds,
    player_sides: dict[str, str],
) -> tuple[list[DamageEvent], int]:
    """Rajaa tapahtumat kierrokseen ja liitä niihin pelaajien puolet.

    Returns:
        ``(tapahtumat, montako jäi ilman puolta)``. Jälkimmäinen luku päätyy
        diagnostiikkaan: äänettömästi hylätty vahinko voisi viedä kierrokselta
        ensikontaktin, eikä mikään kertoisi siitä.
    """
    if bounds.freeze_end_tick is None or bounds.end_tick is None:
        return [], 0
    start, end = bounds.freeze_end_tick, bounds.end_tick

    result: list[DamageEvent] = []
    unknown_sides = 0
    for tick, attacker, victim, weapon in events:
        if not start <= tick <= end:
            continue
        attacker_side = player_sides.get(attacker) if attacker else None
        victim_side = player_sides.get(victim) if victim else None
        # Maailman aiheuttama vahinko (attacker None) on tunnettu tapaus eikä
        # puuttuva havainto, joten sitä ei lasketa tuntemattomaksi.
        if (attacker and attacker_side is None) or (victim and victim_side is None):
            unknown_sides += 1
        result.append(
            DamageEvent(
                tick=tick,
                attacker_id=attacker,
                victim_id=victim,
                weapon=weapon,
                attacker_side=attacker_side,
                victim_side=victim_side,
            )
        )
    return result, unknown_sides


def _sorted_points(points: list[SamplePoint]) -> list[SamplePoint]:
    """Näytepisteet vakaassa järjestyksessä.

    ``sample_kind`` on avaimessa, koska ensikontakti voi osua tasan
    konfiguroidulle sekunnille. Ilman sitä kahden rivin järjestys riippuisi
    syötejärjestyksestä, ja sama demo tuottaisi eri tavut eri ajoilla.
    """
    return sorted(points, key=lambda p: (p.round_raw, p.sample_t_s, p.sample_kind))


def _require_previous(
    previous: tuple[str, str] | None, segment: _Segment, reason: str
) -> tuple[str, str]:
    """Palauta edellisen kierroksen puolikuvaus tai keskeytä.

    Oletus ``("T", "CT")`` olisi arvaus, joka näyttäisi toimivan mutta
    kohdistaisi kierroksen havainnot väärälle joukkueelle.
    """
    if previous is not None:
        return previous
    raise ParseError(
        f"Kierroksen (freeze_end_tick={segment.freeze_end_tick}, "
        f"round_end_tick={segment.end_tick}) puolia ei voitu määrittää: {reason}, "
        "eikä edellistä kierrosta ole, josta kuvauksen voisi periä.\n"
        "Puolen arvaaminen kohdistaisi kierroksen havainnot väärälle "
        "joukkueelle, joten parsinta keskeytetään. Demo on todennäköisesti "
        "vioittunut."
    )


# -- Pieniä muuntimia ---------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:  # pragma: no cover - vertailukelvoton tyyppi
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:  # pragma: no cover
        return None
    text = str(value).strip()
    return text or None


def _as_inventory(value: Any) -> tuple[str, ...] | None:
    """Tavaraluettelo yhdeltä tickiltä.

    Returns:
        Nimet järjestyksessä, tai ``None`` jos propia ei saatu luettua. Tyhjä
        monikko ja ``None`` ovat **eri asioita**: edellinen sanoo "luettiin,
        eikä mitään ollut", jälkimmäinen "ei luettu". Vain jälkimmäinen saa
        jättää kalustolaskurin tyhjäksi.
    """
    if value is None:
        return None
    if isinstance(value, float):  # pandas nostaa puuttuvan arvon NaN:ksi
        return None
    if isinstance(value, str):  # yksittäinen nimi ilman listaa
        text = _as_str(value)
        return () if text is None else (text,)
    try:
        items = list(value)
    except TypeError:
        return None
    names = [_as_str(item) for item in items]
    return tuple(name for name in names if name is not None)


def _as_side(value: Any) -> str | None:
    text = _as_str(value)
    if text is None:
        return None
    text = text.upper()
    return text if text in ("T", "CT") else None


#: Propit, joiden on oltava luettavissa, jotta pelaaja lasketaan mukaan
#: freezetimen lopun summiin ja niiden jakajaan.
_FREEZE_END_PROPS: tuple[str, ...] = (
    "account",
    "cash_spent",
    "equip_freeze_end",
    "equip_round_start",
)


def _readable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pelaajat, joiden freezetimen lopun arvot ovat kaikki luettavissa.

    Sekä summa että sen jakaja lasketaan **tästä samasta joukosta**. Jos
    summattaisiin vain luettavat mutta jaettaisiin kaikilla riveillä, kolmen
    pelaajan varustearvo jaettuna viidellä aliarvioisi tuloksen 40 prosenttia
    ja työntäisi kierroksen ecoksi -- hiljaa ja uskottavan näköisesti.
    """
    return [
        r for r in rows if all(r.get(name) is not None for name in _FREEZE_END_PROPS)
    ]


#: Propit, joiden on oltava luettavissa, jotta kalustolaskurin voi laskea.
#: Nämä **eivät** ole :data:`_FREEZE_END_PROPS`issa: pelaaja pysyy summissa ja
#: niiden jakajassa, vaikka nämä puuttuisivat, koska jakajan on oltava sama
#: joukko kaikille rivin luvuille.
_ARMED_PROPS: tuple[str, ...] = ("armor_value", "inventory")


def _armed_readable(row: dict[str, Any]) -> bool:
    """Ovatko pelaajan panssari ja tavaraluettelo luettavissa.

    Tyhjä tavaraluettelo (``()``) on **havainto**: pelaajalla ei ollut mitään.
    Puuttuva (``None``) ei ole. Sama koskee panssaria: ``0`` on havainto,
    ``None`` ei.
    """
    return all(row.get(name) is not None for name in _ARMED_PROPS)


def _is_armed(row: dict[str, Any]) -> bool:
    """Onko pelaajalla panssari ja vähintään yksi ase hallussa.

    Käyttäjän määritelmä on "kevlar **ja** jokin parannettu ase". Kevlar ilman
    asetta ei riitä eikä ase ilman kevlaria. ``armor_value > 0`` riittää;
    kypärää ei vaadita, koska CT ostaa usein pelkän kevlarin AK:n
    kertaosuman takia.

    Ratkaisee **hallussapito, ei ostos**: säästetty tai poimittu kivääri
    laskeutuu samoin kuin ostettu. Oletuspistoolit ovat silti ulkona, koska ne
    saa joka kierros ilmaiseksi.

    Kutsuja on jo varmistanut :func:`_armed_readable`illa, että arvot ovat
    luettavissa -- tässä ``None`` tulkittaisiin "ei panssaria" ja "ei
    tavaroita", eli lukuvirhe näyttäisi säästöltä.

    Tuntematon nimi ei ole ase (ks. :data:`~pappascout.constants.ARMING_WEAPONS`).
    """
    if not (row.get("armor_value") or 0) > 0:
        return False
    return any(name in ARMING_WEAPONS for name in row.get("inventory") or ())


def _armed_count(own_freeze: list[dict[str, Any]]) -> int | None:
    """Montako pelaajaa oli aseistettu freezetimen lopussa.

    Aseistettu = **panssari ja vähintään yksi ase hallussa**. Joukkuesumma ei
    kerro tätä: kaksi AK:ta ja kolme tyhjää antaa saman summan kuin viisi
    puolinaista, eikä varustearvo ylipäätään erota asetta panssarista ja
    kranaateista. Laskuri lasketaan **samasta joukosta** kuin summat ja
    ``players_freeze_end`` (ks. :func:`_readable`), joten rivillä on vain yksi
    jakaja.

    Args:
        own_freeze: :func:`_readable`-suodatettu joukkueen pelaajajoukko.

    Returns:
        Aseistettujen määrä, tai ``None`` jos lukua ei voi antaa.

        **Nolla ei ole puuttuva havainto**: se on tieto siitä, ettei kukaan
        ollut aseistettu -- täysi eco tuottaa nollan, ja se on aineistoa.

        ``None`` on kaksi eri asiaa, ja molemmat ovat "ei tiedetä":

        * joukko on tyhjä (kierros ilman freezetime-ankkuria), tai
        * **yhdenkin** pelaajan panssari tai tavaraluettelo on lukukelvoton.

        Jälkimmäinen tyhjentää koko rivin eikä vain pudota yhtä pelaajaa,
        koska pelaaja pysyy silti ``players_freeze_end``in jakajassa: "3/5"
        väittäisi, että kaksi oli aseetonta, vaikka totuus on ettei heitä
        saatu luettua. Vaiettu lukuvirhe näyttäisi säästökierrokselta.
    """
    if not own_freeze:
        return None
    if not all(_armed_readable(row) for row in own_freeze):
        return None
    return sum(1 for row in own_freeze if _is_armed(row))


def _sum_or_none(values: list[int | None]) -> int | None:
    """Summaa arvot; ``None`` jos yhtään havaintoa ei ole."""
    valid = [v for v in values if v is not None]
    return sum(valid) if valid else None


def _sum_or_zero(values: list[int | None]) -> int:
    """Summaa arvot; tyhjä joukko on nolla (kukaan ei jäänyt henkiin)."""
    return sum(v for v in values if v is not None)


def _score_before(
    index: int,
    segments: list[_Segment],
    anchor_score: list[int | None],
    end_score: list[int | None],
) -> int | None:
    """Yhteispistemäärä juuri ennen kierrosta ``index``, kun ankkuri puuttuu.

    Varasääntöä kysytään vain kierrokselta, jolla ei ole omaa
    freezetime-ankkuria. Lähin aiempi lukema kelpaa, mutta **ottelun
    uudelleenaloituksen kohdalla luetaan sen ankkuri eikä lopputickiä**:
    lopputickiä sillä ei ole lainkaan, ja sitä edeltävän kierroksen lukema on
    *nollausta edeltävältä* hetkeltä. Puukkokierroksen jälkeen se olisi 1
    vaikka pistemäärä on juuri nollattu -- silloin uudelleenaloitusta seuraava
    kierros saisi ``score_start == score_end`` ja putoaisi pelattujen joukosta.

    Returns:
        Lukema, tai ``None`` jos yhtään ei löytynyt.
    """
    for back in range(index - 1, -1, -1):
        value = (
            anchor_score[back]
            if segments[back].round_raw is None
            else end_score[back]
        )
        if value is not None:
            return value
    return None


def _total_score(rows: list[dict[str, Any]]) -> int | None:
    """Joukkueiden yhteispistemäärä yhdessä tickissä.

    Summa kestää puoliajan vaihdon: joukkuekohtaiset pisteet vaihtavat paikkaa,
    mutta summa säilyy ja kasvaa vain pelatusta kierroksesta.

    Vaatii **molempien** puolten lukeman. Yksipuolinen summa näyttäisi
    kelvolliselta luvulta mutta olisi liian pieni, jolloin kierros voisi pudota
    pelattujen joukosta -- tai pysyä mukana väärällä numerolla.
    """
    per_side: dict[str, int] = {}
    for row in rows:
        if row["team_score"] is not None:
            per_side.setdefault(row["side"], row["team_score"])
    if len(per_side) != len(TEAM_SIDES):
        return None
    return sum(per_side.values())

