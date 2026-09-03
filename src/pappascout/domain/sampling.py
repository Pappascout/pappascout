"""Näytepisteet, ensikontakti ja poikkeamasäännöt (AD-5, AD-10).

Asetelma poimitaan **useassa hetkessä**, ei yhtenä pysäytyskuvana: 6 s (CT:n
suunta näkyy), 15 s (T:n rush erottuu defaultista), 30 s (asetelma), 45 s
(kehitys) sekä **ensikontakti**. Ajat ovat asetus (``[parse].snapshot_seconds``),
eivät koodia.

Näytepiste on aikaperusteinen mutta rajattu kierrokseen
-------------------------------------------------------
Sekunnit muunnetaan tickeiksi ``freeze_end_tick + t x tick_rate``, ja piste
**hylätään, jos se osuisi kierroksen päättymisen jälkeen**. Tämä on ainoa
kohta, jossa kierroksen kesto vaikuttaa näytteistykseen -- ja se on syy, miksi
pisteitä voi olla eri määrä eri kierroksilla. Jos kierros ratkeaa 30
sekunnissa, 45 sekunnin pistettä ei ole olemassa eikä sitä saa keksiä;
aggregointi ottaa sen huomioon otannassa (Story 2.3).

``t_s`` lasketaan aina **valitusta tickistä** eikä nimellisestä sekunnista:
``t_s = (tick - freeze_end_tick) / tick_rate``. Ne eroavat toisistaan
pyöristyksen verran, ja rivillä on molemmat -- ``sample_t_s`` kertoo, mihin
näytepisteeseen rivi kuuluu, ``t_s`` sen todellisen hetken.

Ensikontakti on oma näytepisteensä
----------------------------------
Se ei ole aikapiste vaan tapahtuma: ensimmäinen ``player_hurt``, jossa tekijä
on **vastapuolella** eikä ase ole utilityä. Utilityvahinko ei ole kontakti --
molotov palaa nurkan takana eikä paljasta asetelmaa samalla tavalla kuin
ensimmäinen luoti. Jos kelvollista ``player_hurt``-tapahtumaa ei löydy,
varalähde on ensimmäinen ``player_death`` samoilla ehdoilla.

Kolme poikkeamasääntöä, kolme eri kysymystä
-------------------------------------------
Story 2.5 lisäsi kaksi sääntöä: :func:`ct_advance_hits` ja :func:`crunch_hits`.
Molemmat vastaavat samaan kysymykseen -- **onko subjektin CT-pelaaja
alueella, joka on siinä demossa T:n hallussa** -- ja jakavat siksi saman
orientaatiolaskennan (:func:`t_side_shares`): kahdella laskennalla ne voisivat
olla eri mieltä siitä, kumman aluetta alue on.

**Kumpikaan näistä kahdesta ei sisällä toista.** Crunch lisää
orientaatioehtoon suuntavaatimuksen mutta **pudottaa kierrostyyppirajauksen**,
joten osumajoukot leikkaavat toisiaan: säästökierroksella crunch tuottaa myös
etenemisosuman, täydellä ostolla vain crunchin (mitattu: MatureMayhem Anubis
k10). Kumpaakaan ei siis saa kuvata toisen "tiukempana muotona".

Orientaatio **tulee argumenttina** eikä lasketa täällä. Se on demon oma
havainto alueen elossa-havainnoista aikanäytepisteillä, ja se on laskettava
**suodattamattomasta** näytepistetaulusta eli molempien joukkueiden riveistä.
Tämä on mitattu ehto eikä mieltymys: subjektin omilla riveillä laskettuna
jokainen tosi positiivinen katoaa, koska poikkeama syö oman havaitsemisensa
(:class:`AreaObservations`).

Story 2.14 lisää kolmannen, :func:`stack_hits`. Se **ei lue orientaatiota
lainkaan**: se kysyy, onko subjektin oma puolustus kasautunut yhden siten
ryhmään. Sen johdettu syöte on :func:`site_groups` -- kuvaus
``alue -> "A" | "B"`` demon omasta pistepilvestä -- ja se on tässä samassa
moduulissa säännön kanssa, jotta sääntö ja sen syöte eivät voi olla eri
mieltä. Sama peruste kuin orientaatiolla: **ei karttatietokantaa, ei ihmisen
antamaa aluejakoa, ei arkiston yli kertyvää taulua**. Karttuva lähde antaisi
samalle demolle eri tuloksen sen mukaan, mitä muita demoja arkistossa on.

Kolme sääntöä ovat kolme eri kysymystä samasta havainnosta, eikä yksikään ole
toisen tiukempi tai löysempi muoto.

Tyhjä tulos on **kelvollinen tulos** eikä puute: demo, jossa ei ole
poikkeamia, on havainto siitä ettei poikkeamia ollut.

Moduuli on puhdas: ei tiedostoja, ei demoparser2:ta, ei asetuksia. Sen voi
siksi testata käsin rakennetuilla tietueilla, ja jokainen I/O-matriisin rivi on
täällä yhden funktiokutsun päässä.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median

from pappascout.constants import (
    SAVING_ROUND_TYPES,
    SIDES,
    SITE_AREAS,
    SITE_GROUPS,
)

__all__ = [
    "RoundBounds",
    "SamplePoint",
    "DamageEvent",
    "TIME_SAMPLE",
    "FIRST_CONTACT_SAMPLE",
    "normalize_weapon",
    "normalize_area",
    "seconds_since_freeze_end",
    "sample_ticks",
    "first_contact_tick",
    "CT_ADVANCE",
    "CRUNCH",
    "STACK",
    "SITE_AREAS",
    "SITE_GROUPS",
    "SPAWN_AREAS",
    "AreaObservations",
    "AreaPresence",
    "AnomalyHit",
    "CloudCell",
    "t_side_shares",
    "site_groups",
    "ct_advance_hits",
    "crunch_hits",
    "stack_hits",
    "RULE_SIDE",
]

#: Aikaperusteinen näytepiste.
TIME_SAMPLE = "time"
#: Ensimmäisen ristiinpuolisen osuman hetki.
FIRST_CONTACT_SAMPLE = "first_contact"

# Molempien on oltava SAMPLE_KINDS-luettelossa. Vastaavuutta ei tarkisteta
# tässä moduulitason assertilla -- se katoaisi python -O:lla juuri silloin, kun
# sitä tarvittaisiin. Tarkistus on testissä test_sampling.py.


@dataclass(frozen=True)
class RoundBounds:
    """Yhden kierroksen rajat tickeinä.

    Attributes:
        round_raw: Demon oma kierrosnumero. Kulkee näytepisteen mukana, jotta
            vaihe voi liittää siihen ``round_no``:n -- numeroinnin omistaa
            edelleen vain :mod:`pappascout.domain.rounds`.
        freeze_end_tick: Kierroksen **viimeinen** ``round_freeze_end``. Sama
            ankkuri kuin ``rounds``-taulussa. ``None`` = ankkuria ei ole,
            jolloin ``t_s`` ei ole määritelty eikä kierrosta näytteistetä.
        end_tick: Kierroksen ratkeamishetki. ``None`` = kierros ei ratkennut,
            jolloin sen kestoa ei tunneta eikä pisteitä voi rajata kierrokseen.
    """

    round_raw: int
    freeze_end_tick: int | None
    end_tick: int | None

    @property
    def is_samplable(self) -> bool:
        """Onko kierroksella sekä ankkuri että loppu, ja tässä järjestyksessä."""
        return (
            self.freeze_end_tick is not None
            and self.end_tick is not None
            and self.end_tick >= self.freeze_end_tick
        )


@dataclass(frozen=True)
class SamplePoint:
    """Yksi hetki yhdellä kierroksella; siitä syntyy rivi jokaiselle pelaajalle.

    Attributes:
        round_raw: Kierros, jolle piste kuuluu.
        tick: Demon tick, josta pelaajien sijainnit luetaan.
        sample_kind: ``"time"`` tai ``"first_contact"``.
        sample_t_s: Näytepisteen nimellisaika sekunteina. Aikapisteellä
            asetuksen luku, ensikontaktilla sama kuin ``t_s`` -- kummassakin
            tapauksessa se kertoo, mihin hetkeen rivi viittaa.
        t_s: Todellinen aika ankkurista:
            ``(tick - freeze_end_tick) / tick_rate``.
    """

    round_raw: int
    tick: int
    sample_kind: str
    sample_t_s: float
    t_s: float


@dataclass(frozen=True)
class DamageEvent:
    """``player_hurt`` tai ``player_death`` ensikontaktin päättelyä varten.

    Puolet on selvitetty jo ennen tätä funktiota: domain ei tunne pelin
    propinimiä eikä steamid-puoli-kuvausta.

    Attributes:
        tick: Tapahtuman hetki.
        attacker_id: Tekijä. ``None`` = maailma (putoaminen, istuttajaton
            pommi) -- ei kontakti.
        victim_id: Uhri.
        weapon: Aseen nimi sellaisena kuin demo sen antaa.
        attacker_side: Tekijän puoli tällä kierroksella.
        victim_side: Uhrin puoli tällä kierroksella.
    """

    tick: int
    attacker_id: str | None
    victim_id: str | None
    weapon: str | None
    attacker_side: str | None
    victim_side: str | None


def normalize_weapon(weapon: str | None) -> str | None:
    """Siisti aseen nimi vertailua varten.

    Demon nimet ovat pieniä kirjaimia (``hegrenade``, ``molotov``), mutta
    joissakin lähteissä on etuliite ``weapon_``. Vertailu tehdään
    normalisoidusta nimestä, jotta asetustiedoston lista pysyy luettavana.
    """
    if weapon is None:
        return None
    text = str(weapon).strip().lower()
    if text.startswith("weapon_"):
        text = text[len("weapon_") :]
    return text or None


def normalize_area(value: object) -> str | None:
    """Alue havaintona: tyhjä tai pelkkää tyhjämerkkiä oleva nimi on ``None``.

    **Yksi normalisointi kahdelle puoliskolle.** Poikkeamasääntö vertaa
    alueen nimeä kahdesta eri lähteestä: läsnäolorivin ``area`` ja
    orientaatiokartan avain. Jos vain toinen siivotaan, ``" Lobby "`` on
    orientaatiossa eri alue kuin läsnäolossa -- ja sääntö vaikenee sillä
    alueella ilman että mikään kertoo miksi. ``""`` puolestaan selviäisi
    T-alueiden joukkoon ja tuottaisi poikkeaman nimettömälle alueelle.

    ``parse`` kirjoittaa jo nyt tyhjän ``last_place_name``in ``null``:na, mutta
    sopimus sallii merkkijonon eikä vanhalla versiolla kirjoitettu taulu ole
    käynyt sitä sääntöä läpi. Funktio on täällä eikä aggregoinnissa, koska
    sekä domain että ``aggregate``-vaihe tarvitsevat sen -- ja kahdesta
    kopiosta juuri tämä pari erkanisi.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def seconds_since_freeze_end(
    tick: int, freeze_end_tick: int, tick_rate: float
) -> float:
    """``t_s``: sekunnit kierroksen ankkurista tähän tickiin.

    Sama kaava kuin ``rounds``-taulussa, jotta kaikki putken ajat ovat samasta
    origosta: kierroksen **viimeisestä** ``round_freeze_end``-tickistä.
    """
    _check_tick_rate(tick_rate)
    return (tick - freeze_end_tick) / tick_rate


def sample_ticks(
    segments: Iterable[RoundBounds],
    tick_rate: float,
    sample_seconds: Sequence[float],
) -> list[SamplePoint]:
    """Muunna näytepistesekunnit tickeiksi kierrosten rajojen sisällä.

    Args:
        segments: Kierrosten rajat. Kierros, jolta puuttuu freezetime-ankkuri
            tai päättymistick, ohitetaan kokonaan: ilman ankkuria ``t_s`` ei
            ole määritelty, ja ilman loppua pistettä ei voi rajata kierrokseen.
        tick_rate: Demon tickrate.
        sample_seconds: Näytepisteet sekunteina ankkurista. Järjestyksellä ei
            ole väliä; tulos on aina ajan mukaan nousevassa järjestyksessä.

    Returns:
        Näytepisteet järjestyksessä ``(round_raw, sample_t_s)``. **Kierroksen
        päättymisen jälkeisiä pisteitä ei ole**: jos kierros ratkeaa 28
        sekunnissa, 30 ja 45 sekunnin pisteitä ei synny, ja erittäin lyhyt
        kierros ei tuota yhtään aikapistettä.

    Raises:
        ValueError: Jos tickrate ei ole positiivinen tai jokin näytepiste on
            negatiivinen. Negatiivinen sekuntimäärä osoittaisi freezetimen
            sisään, jossa pelaajat eivät ole vielä liikkuneet.
    """
    _check_tick_rate(tick_rate)
    seconds_list = _unique_sorted_seconds(sample_seconds)

    points: list[SamplePoint] = []
    for bounds in segments:
        if not bounds.is_samplable:
            continue
        freeze_end = bounds.freeze_end_tick
        end = bounds.end_tick
        assert freeze_end is not None and end is not None  # is_samplable
        for seconds in seconds_list:
            tick = freeze_end + round(seconds * tick_rate)
            if tick > end:
                # Kierros ratkesi ennen tätä hetkeä: pistettä ei ole olemassa.
                continue
            points.append(
                SamplePoint(
                    round_raw=bounds.round_raw,
                    tick=tick,
                    sample_kind=TIME_SAMPLE,
                    sample_t_s=seconds,
                    t_s=seconds_since_freeze_end(tick, freeze_end, tick_rate),
                )
            )
    return points


def first_contact_tick(
    hurt_events: Iterable[DamageEvent],
    round_bounds: RoundBounds,
    *,
    exclude_weapons: Collection[str] = (),
    death_events: Iterable[DamageEvent] = (),
    fallback_death: bool = True,
) -> int | None:
    """Kierroksen ensimmäisen ristiinpuolisen osuman tick.

    Kelpaava osuma täyttää kaikki ehdot:

    * tapahtuu kierroksen rajojen sisällä (ankkurista päättymiseen),
    * tekijä ja uhri ovat **eri puolilla** -- oma vahinko ja itsensä
      vahingoittaminen eivät ole kontakti,
    * ase ei ole utilityä.

    Args:
        hurt_events: Kierroksen ``player_hurt``-tapahtumat, järjestys vapaa.
        round_bounds: Kierroksen rajat. Ilman ankkuria tai päättymistickiä
            palautetaan ``None``: hetkeä ei voisi suhteuttaa kierrokseen.
        exclude_weapons: Aseet, jotka eivät kelpaa kontaktiksi
            (``[parse].first_contact_exclude_weapons``). Vertailu tehdään
            normalisoidusta nimestä.
        death_events: Varalähteen ``player_death``-tapahtumat.
        fallback_death: Saako varalähdettä käyttää
            (``[parse].first_contact_fallback_death``).

    Returns:
        Tick tai ``None``, jos kierroksella ei ollut kontaktia. ``None`` on
        oikea vastaus eikä virhe: kierros voi ratketa ajan loppumiseen tai
        pelkkään utilityvahinkoon, ja silloin ensikontaktirivejä ei ole.
    """
    if not round_bounds.is_samplable:
        return None
    excluded_weapons = {
        w
        for w in (normalize_weapon(name) for name in exclude_weapons)
        if w is not None
    }

    hit = _first_matching(hurt_events, round_bounds, excluded_weapons)
    if hit is not None:
        return hit
    if not fallback_death:
        return None
    return _first_matching(death_events, round_bounds, excluded_weapons)


# -- Poikkeamasäännöt (AD-10, Story 2.5) --------------------------------------


#: CT-eteneminen: subjektin CT-pelaaja alueella, joka on **siinä demossa** T:n
#: hallussa, säästökierroksella.
CT_ADVANCE = "ct_advance"

#: Puoli, jonka rivejä poikkeamasäännöt tutkivat.
#:
#: Kaikki kolme sääntöä kysyvät, mitä **subjekti tekee CT:nä**, joten T-puolen
#: rivit eivät voi tuottaa osumaa yhdelläkään. Vakiona siksi, että sama arvo
#: tarvitaan kahdessa paikassa: rivien suodatuksessa (:func:`_is_ct_time_row`)
#: ja aggregoinnin kattavuusluvussa, joka kertoo montako kierrosta sääntö
#: **voi** osua. Kahtena kirjoitettuna kattavuus voisi luvata enemmän kuin
#: sääntö tutkii.
RULE_SIDE = "CT"

#: Crunch: sama alue, mutta vähintään kaksi pelaajaa **saapuneena** vähintään
#: kahdesta eri suunnasta yhtä aikaa -- **millä tahansa kierrostyypillä**.
#: Sama orientaatioehto kuin etenemisellä, yksi lisävaatimus ja yksi rajaus
#: vähemmän, joten sääntöjen osumajoukot leikkaavat toisiaan eikä kumpikaan
#: sisällä toista.
CRUNCH = "crunch"

#: Stack: vähintään ``min_players`` subjektin elossa olevaa CT-pelaajaa saman
#: siten ryhmässä, ja vähintään yksi heistä siten **omalla** alueella.
#:
#: Sääntö ei lue orientaatiota eikä kierrostyyppiä. Se on kolmas kysymys
#: samasta havainnosta, ei kahden muun muunnelma.
STACK = "stack"

#: Alueet, jotka **eivät kelpaa** stackin laskentaan.
#:
#: Spawnissa seisova ei puolusta sitea. Rajaus on määritelmää eikä siivousta:
#: ``CTSpawn`` osuu Ancientilla A-ryhmään ja Infernolla B-ryhmään, joten ilman
#: sitä pelkkä **aloitusasetelma** laukaisisi säännön molemmilla kartoilla --
#: eli sääntö mittaisi kierroksen alkua eikä puolustuksen valintaa.
#:
#: ``TSpawn`` on mukana samasta syystä: se on Anubiksella B-ryhmässä, ja
#: CT-pelaaja siellä on jo eri havainto (``ct_advance``), ei stack.
SPAWN_AREAS: frozenset[str] = frozenset({"CTSpawn", "TSpawn"})


@dataclass(frozen=True)
class AreaObservations:
    """Yhden alueen elossa-havainnot demon **suodattamattomasta** taulusta.

    Orientaatio on demon oma havainto: ei karttatietokantaa, ei ihmisen
    antamaa aluejakoa, ei arkiston yli kertyvää taulua. Karttuva lähde antaisi
    samalle demolle eri tuloksen sen mukaan, mitä muita demoja arkistossa on.

    **Molempien joukkueiden rivit, ei vain subjektin.** Tämä on mitattu ehto
    eikä mieltymys: kun subjekti etenee alueelle CT:nä, hänen omat
    CT-havaintonsa laskevat sen alueen T-osuutta -- poikkeama syö oman
    havaitsemisensa. Subjektin riveillä laskettuna kolme aluetta putoaa
    kynnyksen alle (0,88 -> 0,79, 0,85 -> 0,75, 0,84 -> 0,75), ja ne ovat
    täsmälleen ne kolme, jotka tuottivat kaikki oikeat osumat.

    Attributes:
        t: Havainnot, joissa rivin puoli oli ``T``.
        total: Alueen kaikki elossa-havainnot aikanäytepisteillä.

    Raises:
        ValueError: Jos luvut ovat mahdottomia. ``total = 0`` ei ole alue
            vaan alueen puuttuminen, eikä siitä voi laskea osuutta lainkaan.
    """

    t: int
    total: int

    def __post_init__(self) -> None:
        if self.total <= 0:
            raise ValueError(
                f"Alueella on {self.total} havaintoa, joten sillä ei ole "
                "orientaatiota. Nolla havaintoa ei ole alue vaan alueen "
                "puuttuminen, eikä T-osuutta voi laskea."
            )
        if not 0 <= self.t <= self.total:
            raise ValueError(
                f"T-havaintoja on {self.t} kun havaintoja on yhteensä "
                f"{self.total}; osajoukko ei voi olla joukkoa suurempi."
            )

    @property
    def t_share(self) -> float:
        """T-havaintojen osuus alueen kaikista havainnoista."""
        return self.t / self.total


@dataclass(frozen=True)
class AreaPresence:
    """Yhden pelaajan läsnäolo yhdellä näytepisteellä yhdellä kierroksella.

    Rivi on ``TICKS``-taulun rivi ilman koordinaatteja: säännöt lukevat vain
    alueen, ja koordinaatit houkuttelisivat geometriaan, jota ei ole.

    Attributes:
        player_id: Pelaaja. Osumien pelaajamäärä lasketaan **eri
            pelaajista**, ei riveistä.
        side: Rivin joukkueen puoli tällä kierroksella. Säännöt tutkivat vain
            ``CT``-rivejä, joten sama pelaaja T:nä ei voi tuottaa osumaa.
        sample_kind: ``"time"`` tai ``"first_contact"``. Vain aikanäytepisteet
            kelpaavat: ensikontaktin ``sample_t_s`` on **mitattu hetki**, joten
            se läpäisisi aikarajan mielivaltaisesti eikä olisi
            vertailukelpoinen kierrosten välillä.
        sample_t_s: Näytepisteen nimellisaika sekunteina.
        area: Pelin oma ``env_cs_place``-alue tai ``None``.
        is_alive: Kuollut pelaaja ei ole alueella.
    """

    player_id: str
    side: str
    sample_kind: str
    sample_t_s: float
    area: str | None
    is_alive: bool = True


@dataclass(frozen=True)
class AnomalyHit:
    """Yksi osuma: sääntö, alue, hetki ja havainnon luvut.

    Osuma on **yhden näytepisteen** havainto yhdellä kierroksella. Sama alue
    voi osua useammalla näytepisteellä ja useammalla kierroksella;
    ryhmittely otannaksi (``n/m``) tehdään aggregoinnissa, ei täällä.

    **Kenttä kuuluu sille säännölle, joka sen mittasi.** Neljä kenttää on
    sääntökohtaisia, ja :meth:`__post_init__` vaatii ne täsmälleen oikealta
    säännöltä. Ilman vartijaa osuma voisi kantaa lukua, jota sen sääntö ei
    laskenut -- ja raportin rivi väittäisi mitatuksi jotain, jota ei mitattu.
    Sama peruste kuin ``sources``illa jo oli: etenemisrivin tyhjä
    lähtöaluelista tarkoittaa "ei kysytty", ei "ei suuntia".

    Attributes:
        rule: :data:`CT_ADVANCE`, :data:`CRUNCH` tai :data:`STACK`.
        area: Alue, jolla osuma havaittiin. Ei koskaan ``None``: alue ilman
            nimeä ei voi olla T:n aluetta. Stackilla se on **siten oma alue**
            (:data:`SITE_AREAS`), koska juuri se on ryhmän ankkuri ja säännön
            lisäehto -- ei se alue, jolla pelaajia sattui olemaan eniten.
        sample_t_s: Näytepiste, jolla osuma havaittiin.
        players: Eri pelaajien määrä. Etenemisessä kaikki alueella olevat
            CT-pelaajat, crunchissa vain **saapuneet** (ks.
            :func:`crunch_hits`), stackissa ryhmässä olevat.
        t_share: Alueen T-osuus tässä demossa. **Vain orientaatiosäännöillä**:
            stack ei lue orientaatiota, joten sillä luku olisi keksitty.
        observations: Alueen havaintojen määrä, eli orientaation oma otanta.
            Sama rajaus kuin ``t_share``illa.
        sources: Crunchin lähtöalueet aakkosjärjestyksessä; muilla tyhjä.
        alive: Subjektin elossa olevat CT-pelaajat **tällä näytepisteellä**.
            Vain stackilla. Se on osuman nimittäjä: neljä viidestä ja neljä
            neljästä ovat eri havainto, ja pelkkä ``players`` ei erota niitä.
        site: Siten ryhmä (:data:`SITE_GROUPS`), jossa pelaajat olivat. Vain
            stackilla.
    """

    rule: str
    area: str
    sample_t_s: float
    players: int
    t_share: float | None = None
    observations: int | None = None
    sources: tuple[str, ...] = ()
    alive: int | None = None
    site: str | None = None

    def __post_init__(self) -> None:
        """Sääntökohtaiset kentät kuuluvat omalle säännölleen.

        Raises:
            ValueError: Jos osuma kantaa kenttää, jota sen sääntö ei mittaa,
                tai jos siltä puuttuu kenttä, jonka sen sääntö mittaa.
                Kumpikin tekisi raportin rivistä väitteen ilman havaintoa.
        """
        orientation_rule = self.rule in (CT_ADVANCE, CRUNCH)
        has_orientation = self.t_share is not None or self.observations is not None
        if orientation_rule and not (
            self.t_share is not None and self.observations is not None
        ):
            raise ValueError(
                f"Osuma {self.rule!r} alueella {self.area!r} ei kanna alueen "
                "orientaatiota. Sääntö nojaa siihen, että alue on T:n "
                "hallussa, joten osuma ilman T-osuutta ja havaintomäärää on "
                "väite ilman todistetta."
            )
        if self.rule == STACK:
            if has_orientation:
                raise ValueError(
                    f"Stack-osuma alueella {self.area!r} kantaa alueen "
                    "orientaatiota, vaikka sääntö ei lue sitä lainkaan. "
                    "Luku näyttäisi mitatulta muttei koskisi tätä osumaa."
                )
            if self.site not in SITE_GROUPS:
                raise ValueError(
                    f"Stack-osuman siteryhmä on {self.site!r}; sallitut ovat "
                    f"{list(SITE_GROUPS)}. Ryhmä on osuman ankkuri, eikä "
                    "sitä voi jättää nimeämättä."
                )
            if self.alive is None:
                raise ValueError(
                    f"Stack-osuma alueella {self.area!r} ei kerro, montako "
                    "pelaajaa oli elossa. Neljä viidestä ja neljä neljästä "
                    "ovat eri havainto, eikä pelkkä pelaajamäärä erota niitä."
                )
            if not 0 < self.players <= self.alive:
                raise ValueError(
                    f"Stack-osuma väittää {self.players} pelaajaa ryhmässä, "
                    f"kun elossa on {self.alive}. Ryhmässä olevat ovat "
                    "osajoukko elossa olevista."
                )
        else:
            if self.alive is not None or self.site is not None:
                raise ValueError(
                    f"Osuma {self.rule!r} alueella {self.area!r} kantaa "
                    "stackin kenttiä (elossa, siteryhmä), vaikka sääntö ei "
                    "mittaa niitä."
                )
        if self.sources and self.rule != CRUNCH:
            raise ValueError(
                f"Osuma {self.rule!r} alueella {self.area!r} kantaa "
                "lähtöalueita, vaikka vain crunch laskee suuntia."
            )


@dataclass(frozen=True)
class CloudCell:
    """Yksi pistepilven ruutu: missä kartalla on seisottu ja mikä alue se on.

    ``CALLOUT_CLOUD``-taulun rivi ilman ``map_demo_id``:tä ja
    ``observations``ia. Kumpikin jätetään pois tarkoituksella:

    * demon tunniste on kutsujan kirjanpitoa, ei säännön syötettä;
    * **havaintomäärä ei paina** aluekeskipisteessä. Jokainen ruutu painaa
      yhden, ja se on mitattu ehto eikä yksinkertaistus -- ks.
      :func:`site_groups`.

    Attributes:
        area: Ruudun alue pelin omalla nimellä (``env_cs_place``).
        cell_x: Ruudun indeksi, ``floor(x / [parse].callout_grid_units)``.
        cell_y: Sama y-akselilla.
        cell_z: Sama z-akselilla. **Mukana eikä sivuutettuna**: Nuke on
            kerroksellinen, ja siteet erottaa siellä vain pystyero.
    """

    area: str
    cell_x: int
    cell_y: int
    cell_z: int


def t_side_shares(
    orientation: Mapping[str | None, AreaObservations],
    *,
    t_share_min: float,
    min_observations: int,
) -> dict[str, AreaObservations]:
    """Alueet, jotka ovat **tässä demossa** T:n hallussa.

    Molemmat poikkeamasäännöt lukevat alueen puoliorientaation tästä samasta
    funktiosta. Se ei ole koodin säästöä vaan määritelmä: säännöt kysyvät
    saman kysymyksen, ja kahdella laskennalla ne voisivat olla eri mieltä
    siitä, kumman aluetta alue on.

    Args:
        orientation: Alue -> sen havainnot. Avain ``None`` (alueen nimeä ei
            saatu) **ohitetaan**: nimetön alue ei voi olla kumman tahansa
            puolen aluetta, ja osuma "tuntemattomalla alueella" ei kertoisi
            mistä.
        t_share_min: ``[thresholds].advance_t_share``.
        min_observations: ``[thresholds].advance_area_min_observations``.
            Vertailu on ``>=``: **rajalla oleva alue kelpaa**, ja tasan 20
            havainnon alue on siis mukana. Tarkkuus on tässä kantavaa, koska
            koko kynnysten kalibrointi nojaa tasarajoihin (Nuken piha on
            tasan 0,70). Alue, joka **alittaa** rajan, ei ole T:n eikä CT:n
            aluetta -- orientaatiota ei arvata ohuesta havainnosta.

    Returns:
        Alue -> havainnot, vain kynnykset ylittäneistä alueista.

    Raises:
        ValueError: Jos ``t_share_min`` ei ole välillä 0..1 tai
            ``min_observations`` ei ole positiivinen. Kumpikin tekisi säännön
            hiljaa mahdottomaksi tai laukaisisi sen joka alueella.
    """
    if not 0.0 <= t_share_min <= 1.0:
        raise ValueError(
            f"T-osuuden kynnys {t_share_min!r} ei ole välillä 0..1. Osuus on "
            "T-havaintojen määrä jaettuna alueen kaikilla havainnoilla, joten "
            "sen ulkopuolinen kynnys joko hiljentäisi säännön kokonaan tai "
            "tekisi jokaisesta alueesta T:n aluetta."
        )
    if min_observations < 1:
        raise ValueError(
            f"Alueen vähimmäishavaintomäärä {min_observations!r} ei ole "
            "positiivinen. Ilman havaintoa alueella ei ole orientaatiota, "
            "eikä sitä saa arvata."
        )
    seen_raw: set[str] = set()
    passed: dict[str, AreaObservations] = {}
    for raw_area, obs in orientation.items():
        area = normalize_area(raw_area)
        if area is None:
            continue
        if area in seen_raw:
            raise ValueError(
                f"Orientaatiokartassa on alue {area!r} kahdesti eri "
                f"kirjoitusasussa. Alueen nimi on havainto, joten kahdesta "
                "asusta ei voi valita -- kirjoita kartta yhdellä "
                "normalisoinnilla (domain.sampling.normalize_area)."
            )
        seen_raw.add(area)
        if obs.total >= min_observations and obs.t_share >= t_share_min:
            passed[area] = obs
    return passed


def site_groups(
    cells: Iterable[CloudCell],
    *,
    margin: float,
    separation_min: float,
) -> dict[str, str] | None:
    """Kuvaus ``alue -> "A" | "B"`` **demon omasta pistepilvestä**.

    Tämä on stack-säännön puuttunut pala. Peli jakaa siten useaan alueeseen
    (Ancientin B on ``Alley`` + ``BombsiteB`` + ``SideEntrance``), joten neljä
    puolustajaa eivät koskaan ole samalla ``env_cs_place``-alueella: sääntö
    tarvitsee ryhmän, ja ryhmä **johdetaan** eikä anneta. Sama lukittu ehto
    kuin :class:`AreaObservations`illa -- ei karttatietokantaa, ei ihmisen
    antamaa aluejakoa, ei arkiston yli kertyvää taulua.

    Menetelmä on kolmiosainen:

    1. **Aluekeskipiste on solumediaani**: jokainen ruutu painaa yhden,
       havaintomäärä ei paina. Tämä on mitattu ehto. Pelin
       ``m_szLastPlaceName`` on *viimeksi nimetty* alue, joten nimeämättömässä
       kohdassa seisova pelaaja kantaa edellisen kierroksen aluetta;
       ``Ancient_vs_kaljukostaja``n CT-spawnin ruuduissa on ``BombsiteB``
       75 524 havaintoa ja ``CTSpawn`` vain 135. Havaintopainotettu keskiarvo
       vetää siten keskipisteen spawniin, solumediaani ei -- 75 000 havaintoa
       16 ruudussa painaa 16 ruudun verran. Mitattu: Ancientin ristiriitaiset
       alueet 5/18 -> **0/18**, ja aluejako on sanatarkasti sama kaikista
       kolmesta Ancient-demosta.
    2. **Alueen koko on ruutujen mediaanietäisyys** omasta keskipisteestään.
       Mediaani eikä keskiarvo tai suurin: yksi vanhentunut nimi kartan
       toisella laidalla venyttäisi molempia, ja juuri se vika tässä
       torjutaan.
    3. **Ryhmä on lähempi site marginaalilla**: alue kuuluu lähempään siteen
       vain, jos toinen site on vähintään ``margin``-kertaa kauempana. Muuten
       alue jää ryhmättömäksi (kartan jaettu keski), eikä suuntaa arvata.

    **Kartta, jolla siteet eivät erotu, vaikenee.** Nukella ``BombsiteA`` ja
    ``BombsiteB`` ovat päällekkäin eri kerroksissa, joten *mikä tahansa*
    A/B-etäisyysmittari on siellä mieletön. Vartija on suhdeluku eikä
    karttalista: siteiden keskipisteiden etäisyys jaettuna siteiden omalla
    koolla on 0,47-0,54 Nukella ja 3,70-5,04 kolmella muulla kartalla, joten
    kynnys 2,0 erottaa ne puhtaasti **ilman että karttaa nimetään koodissa**.
    Vaikeneminen on oikea vastaus eikä puute -- mutta se on kirjattava
    kattavuuteen (``AnomalyScan.demos_without_site_groups``), ei jätettävä
    hiljaiseksi.

    **Ruudukon koko supistuu pois, ja siksi sitä ei anneta.** Ruutuindeksi ei
    ole koordinaatti: oikea koordinaatti on ``ruutu * [parse].callout_grid_units``.
    Molemmat kynnykset ovat kuitenkin **kahden etäisyyden osamääriä**, ja
    ruudukon koko kertoo jokaisen etäisyyden samalla luvulla, joten se
    supistuu kummastakin vertailusta pois. Juuri siksi tämä funktio -- ja
    aggregointi sen kutsujana -- ei lue ``[parse]``-osiota lainkaan. **Jos
    tähän joskus lisätään absoluuttinen etäisyysraja, muunnos on pakollinen**,
    eikä sen lähdettä ole vielä olemassa aggregoinnissa.

    Args:
        cells: Demon pistepilven ruudut. Järjestys vapaa; tyhjä pilvi on
            kelvollinen syöte ja tuottaa ``None``.
        margin: ``[thresholds].stack_group_margin``. Vähintään 1,0.
        separation_min: ``[thresholds].stack_site_separation_min``.

    Returns:
        ``alue -> "A" | "B"`` niistä alueista, joilla on ryhmä, tai ``None``
        jos kartalla ei ole tasoerottuvaa A/B-jakoa. Ryhmättömät alueet
        **puuttuvat** kuvauksesta; ``None``-arvoa ei kirjoiteta, jotta
        ``groups.get(area)`` on yksiselitteinen.

        **Tulos ei voi olla tyhjä kuvaus.** Siten etäisyys omaan
        keskipisteeseensä on 0, joten kumpikin site kuuluu aina omaan
        ryhmäänsä millä tahansa marginaalilla; jos funktio pääsee tänne asti,
        kuvauksessa on vähintään ne kaksi. Tyhjä sanakirja on siis
        mahdollinen vain kutsujan omana arvona (esimerkiksi testissä), ei
        tämän funktion tuloksena, eikä koodi saa nojata sen erottamiseen
        ``None``ista.

    Raises:
        ValueError: Jos ``margin`` on alle 1,0 tai ``separation_min`` ei ole
            positiivinen. Edellinen tekisi "lähemmästä" kauemman,
            jälkimmäinen poistaisi vartijan kokonaan -- eli Nuken
            päällekkäisistä siteistä johdettaisiin jako, jota ei ole.
    """
    if not (margin >= 1.0 and math.isfinite(margin)):
        raise ValueError(
            f"Ryhmämarginaali {margin!r} on alle 1,0 tai ei ole äärellinen. "
            "Marginaali kertoo, kuinka paljon kauempana toisen siten on "
            "oltava, ja alle yhden arvolla 'lähempi' site voisi olla "
            "kauempana."
        )
    if not (separation_min > 0.0 and math.isfinite(separation_min)):
        raise ValueError(
            f"Erottuvuuskynnys {separation_min!r} ei ole positiivinen "
            "äärellinen luku. Arvolla 0 vartija ei vaientaisi yhtäkään "
            "karttaa, eli päällekkäisistä siteistä johdettaisiin aluejako, "
            "jota ei ole olemassa."
        )

    points: dict[str, list[tuple[float, float, float]]] = {}
    for cell in cells:
        area = normalize_area(cell.area)
        if area is None:
            # Nimetön ruutu ei nimeä aluetta. Sama sääntö kuin pilven
            # rakentamisessa (domain.utility.point_cloud).
            continue
        points.setdefault(area, []).append(
            (float(cell.cell_x), float(cell.cell_y), float(cell.cell_z))
        )

    centres = {area: _cell_median(pts) for area, pts in points.items()}
    site_a, site_b = SITE_AREAS["A"], SITE_AREAS["B"]
    if site_a not in centres or site_b not in centres:
        # Pilvi, jossa toista sitea ei ole, ei voi kertoa siteiden välistä
        # jakoa. Havainnon puuttuminen ei ole havainto jaon puuttumisesta.
        return None

    span = _spread(points[site_a], centres[site_a]) + _spread(
        points[site_b], centres[site_b]
    )
    separation = math.dist(centres[site_a], centres[site_b])
    # Kolme ehtoa, ja kaksi ensimmäistä ovat vartijan aukkoja eivätkä
    # varmuuden vuoksi -tarkistuksia.
    #
    # ``span <= 0`` on **nollakokoinen site**: molemmilla siteillä on yksi
    # ruutu, jolloin suhde jakaa nollalla ja mikä tahansa erotus läpäisisi
    # kynnyksen. Kahden ruudun havainto ei kerro kartan siterakenteesta
    # mitään, ja demo, jonka pilvi on noin ohut, on rikki eikä
    # tasoerottuva.
    #
    # ``separation <= 0`` on **päällekkäiset keskipisteet**: siteitä ei voi
    # erottaa toisistaan lainkaan, eikä kumpikaan olisi aidosti lähempänä
    # yhtäkään aluetta.
    if span <= 0.0 or separation <= 0.0:
        return None
    if separation < separation_min * span:
        return None

    found: dict[str, str] = {}
    for area, centre in centres.items():
        to_a = math.dist(centre, centres[site_a])
        to_b = math.dist(centre, centres[site_b])
        # Aidosti lähempi ENNEN marginaalia: arvolla margin == 1,0 pelkkä
        # marginaaliehto täyttyisi tasatilanteessa molempiin suuntiin, ja
        # ryhmä ratkeaisi siitä, kumpi haara kirjoitettiin ensin.
        if to_a < to_b and to_b >= margin * to_a:
            found[area] = "A"
        elif to_b < to_a and to_a >= margin * to_b:
            found[area] = "B"
    return found


def ct_advance_hits(
    presences: Iterable[AreaPresence],
    *,
    round_type: str | None,
    orientation: Mapping[str | None, AreaObservations],
    t_share_min: float,
    area_min_observations: int,
    max_sample_s: float,
    min_players: int,
) -> list[AnomalyHit]:
    """Yhden kierroksen CT-etenemiset.

    Sääntö: subjektin CT-pelaaja alueella, joka on **siinä demossa** T:n
    hallussa, säästökierroksella ja enintään ``max_sample_s`` sekunnin
    kohdalla.

    Rajaus säästökierroksiin on **taloudellinen havainto** eikä otannan
    kapeuttamista: köyhä CT ei normaalisti etene T:n alueelle, joten juuri
    silloin eteneminen kertoo suunnitelmasta. Kierrostyypit ovat
    :data:`~pappascout.constants.SAVING_ROUND_TYPES`.

    Args:
        presences: Kierroksen näytepisterivit, järjestys vapaa. Muut kuin
            elossa olevat CT-rivit aikanäytepisteiltä ohitetaan täällä, jotta
            kutsujan ei tarvitse muistaa suodattaa niitä.
        round_type: Kierroksen tyyppi. ``None`` (luokittelematon kierros) ei
            voi osua: ilman tyyppiä ei tiedetä oliko kierros säästökierros.
        orientation: Alue -> havainnot demon **suodattamattomasta** taulusta.
        t_share_min: ``[thresholds].advance_t_share``.
        area_min_observations: ``[thresholds].advance_area_min_observations``.
        max_sample_s: ``[thresholds].advance_max_sample_s``.
        min_players: ``[thresholds].advance_min_players``.

    Returns:
        Osumat järjestyksessä ``(sample_t_s, area)``. Tyhjä lista on
        kelvollinen tulos eikä puute.
    """
    if round_type not in SAVING_ROUND_TYPES:
        return []
    t_areas = t_side_shares(
        orientation,
        t_share_min=t_share_min,
        min_observations=area_min_observations,
    )
    if not t_areas:
        return []
    hits: list[AnomalyHit] = []
    for (seconds, area), players in _players_by_point(
        presences, t_areas, max_sample_s
    ).items():
        if len(players) < min_players:
            continue
        obs = t_areas[area]
        hits.append(
            AnomalyHit(
                rule=CT_ADVANCE,
                area=area,
                sample_t_s=seconds,
                players=len(players),
                t_share=obs.t_share,
                observations=obs.total,
            )
        )
    return sorted(hits, key=lambda hit: (hit.sample_t_s, hit.area))


def crunch_hits(
    presences: Iterable[AreaPresence],
    *,
    orientation: Mapping[str | None, AreaObservations],
    t_share_min: float,
    area_min_observations: int,
    max_sample_s: float,
    min_players: int,
    min_sources: int,
) -> list[AnomalyHit]:
    """Yhden kierroksen crunchit.

    Sääntö lukee saman orientaation kuin :func:`ct_advance_hits`, mutta vaatii
    lisäksi että pelaajat ovat **saapuneet** alueelle vähintään
    ``min_sources`` eri suunnasta yhtä aikaa. Lähtöalue on pelaajan oma alue
    **edellisellä** aikanäytepisteellä eli havainto -- ei karttageometriaa
    eikä aluenaapuruustaulua.

    **Crunch ei rajoitu säästökierroksiin, vaikka eteneminen rajoittuu.**
    Epic asettaa taloudellisen ehdon vain etenemiselle, ja mittaus tukee sitä:
    yksi viidestä crunchista (MatureMayhem Anubis k10) on täysi osto. Sääntö ei
    siis ole etenemisen "tiukempi muoto": se on tiukempi suunnista ja
    löysempi kierrostyypistä, joten osumajoukot leikkaavat toisiaan.

    ``players`` on **saapuneiden** määrä eikä alueella olevien: pelaaja, joka
    oli alueella jo edellisellä näytepisteellä, ei saapunut sinne mistään.
    Sama näytepiste voi siis tuottaa etenemisosuman kolmella pelaajalla ja
    crunch-osuman kahdella, ja se on kaksi eri havaintoa samasta hetkestä.

    Pelaaja, jonka edellinen alue ei ole tiedossa (nimetön alue tai kierroksen
    ensimmäinen näytepiste), **ei ole saapunut mistään**: suuntaa ei arvata.

    Args:
        presences: Kierroksen näytepisterivit, järjestys vapaa. Lähtöalueiden
            takia mukana on oltava **kierroksen kaikki** aikanäytepisteet,
            myös ne, jotka ovat ``max_sample_s``:n jälkeen -- muuten edellinen
            näytepiste voi puuttua ja saapuminen jäisi näkymättä.
        orientation: Kuten :func:`ct_advance_hits`issa.
        t_share_min: ``[thresholds].advance_t_share``, **jaettu** etenemisen
            kanssa.
        area_min_observations: ``[thresholds].advance_area_min_observations``.
        max_sample_s: ``[thresholds].advance_max_sample_s``.
        min_players: ``[thresholds].crunch_min_players``.
        min_sources: ``[thresholds].crunch_min_sources``.

    Returns:
        Osumat järjestyksessä ``(sample_t_s, area)``.
    """
    t_areas = t_side_shares(
        orientation,
        t_share_min=t_share_min,
        min_observations=area_min_observations,
    )
    if not t_areas:
        return []
    rows = [row for row in presences if _is_ct_time_row(row)]
    previous = _previous_areas(rows)

    arrivals: dict[tuple[float, str], dict[str, str]] = {}
    for row in rows:
        area = normalize_area(row.area)
        if area is None or area not in t_areas or row.sample_t_s > max_sample_s:
            continue
        source = previous.get((row.player_id, row.sample_t_s))
        if source is None or source == area:
            # Ei saapunut: suunta on tuntematon tai pelaaja oli jo alueella.
            continue
        arrivals.setdefault((row.sample_t_s, area), {})[row.player_id] = source

    hits: list[AnomalyHit] = []
    for (seconds, area), by_player in arrivals.items():
        sources = sorted(set(by_player.values()))
        if len(by_player) < min_players or len(sources) < min_sources:
            continue
        obs = t_areas[area]
        hits.append(
            AnomalyHit(
                rule=CRUNCH,
                area=area,
                sample_t_s=seconds,
                players=len(by_player),
                t_share=obs.t_share,
                observations=obs.total,
                sources=tuple(sources),
            )
        )
    return sorted(hits, key=lambda hit: (hit.sample_t_s, hit.area))


def stack_hits(
    presences: Iterable[AreaPresence],
    *,
    groups: Mapping[str, str] | None,
    max_sample_s: float,
    min_players: int,
) -> list[AnomalyHit]:
    """Yhden kierroksen stackit.

    Sääntö: **vähintään** ``min_players`` subjektin elossa olevaa CT-pelaajaa
    saman siten ryhmässä yhdellä aikanäytepisteellä, **ja vähintään yksi
    heistä siten omalla alueella** (:data:`SITE_AREAS`).

    Kaksi lisäehtoa eivät ole hienosäätöä vaan määritelmää:

    * **Siten oma alue.** "Stack sitellä" tarkoittaa että ollaan sitellä, ei
      että ollaan kartan siinä puoliskossa. Ilman ehtoa Ancientin ``Alley``
      yksin tuottaa osumia 6 s kohdalla -- se on CT-spawnin uloskäytävä, ei
      site. Mitattu: ehto pudottaa 17 kierrosta -> 9 (26 osumaa -> 10).
    * **Spawnit pois** (:data:`SPAWN_AREAS`). Spawnissa seisova ei puolusta
      sitea, ja ``CTSpawn`` osuu Ancientilla A-ryhmään ja Infernolla
      B-ryhmään -- ilman rajausta pelkkä aloitusasetelma laukaisisi säännön
      molemmilla kartoilla.

    Sääntö **ei rajoitu kierrostyyppiin** eikä lue alueen orientaatiota. Se ei
    siis ole kummankaan toisen säännön tiukempi tai löysempi muoto vaan kolmas
    kysymys samasta havainnosta.

    Args:
        presences: Kierroksen näytepisterivit, järjestys vapaa. Muut kuin
            elossa olevat CT-rivit aikanäytepisteiltä ohitetaan täällä.
        groups: :func:`site_groups`in tulos tälle demolle. ``None``
            (kartalla ei ole tasoerottuvaa A/B-jakoa) **vaientaa säännön**, ja
            se on oikea vastaus eikä puute -- mutta kutsujan on kirjattava se
            kattavuuteen, ei jätettävä hiljaiseksi.
        max_sample_s: ``[thresholds].advance_max_sample_s``. **Yhteinen**
            kahden muun säännön kanssa eikä oma kynnys: kolme sääntöä kysyvät
            samasta havainnosta, ja kahdella aikarajalla ne voisivat olla eri
            mieltä siitä, milloin kierroksen alku loppuu.
        min_players: ``[thresholds].stack_min_players``.

    Returns:
        Osumat järjestyksessä ``(sample_t_s, area)``. Tyhjä lista on
        kelvollinen tulos; ``groups=None`` tuottaa myös tyhjän listan, ja
        **niitä kahta ei voi erottaa täältä** -- ero on kattavuudessa.

    Raises:
        ValueError: Jos ``min_players`` ei ole positiivinen tai jos
            ``groups`` nimeää ryhmän, jota ei ole. Edellinen laukaisisi
            säännön jokaisella näytepisteellä, jälkimmäinen tarkoittaisi että
            ryhmät tulevat muualta kuin :func:`site_groups`ista.
    """
    if min_players < 1:
        raise ValueError(
            f"Stackin vähimmäispelaajamäärä {min_players!r} ei ole "
            "positiivinen. Nollalla sääntö osuisi jokaisella näytepisteellä, "
            "jolla siten ryhmässä ei ole ketään."
        )
    if groups is None:
        return []
    unknown = sorted({name for name in groups.values() if name not in SITE_GROUPS})
    if unknown:
        raise ValueError(
            f"Siteryhmien joukossa on tuntematon ryhmä: {unknown}. Sallitut "
            f"ovat {list(SITE_GROUPS)}, ja ne tulevat site_groups()ista."
        )

    # Elossaolo lasketaan KAIKISTA kelpaavista riveistä, myös spawnissa ja
    # ryhmättömällä alueella olevista: se on osuman nimittäjä ("neljä
    # viidestä"), eikä pelaaja lakkaa olemasta elossa siksi, että hän seisoo
    # väärässä paikassa.
    alive: dict[float, set[str]] = {}
    # (näytepiste, ryhmä) -> pelaaja -> hänen alueensa. Pelaajat ovat avaimia
    # eivätkä rivejä: kaksoisrivi samasta pelaajasta ei saa nostaa
    # pelaajamäärää, koska juuri se on raportin luku.
    members: dict[tuple[float, str], dict[str, set[str]]] = {}
    for row in presences:
        if not _is_ct_time_row(row) or row.sample_t_s > max_sample_s:
            continue
        alive.setdefault(row.sample_t_s, set()).add(row.player_id)
        area = normalize_area(row.area)
        if area is None or area in SPAWN_AREAS:
            continue
        group = groups.get(area)
        if group is None:
            continue
        members.setdefault((row.sample_t_s, group), {}).setdefault(
            row.player_id, set()
        ).add(area)

    hits: list[AnomalyHit] = []
    for (seconds, group), by_player in members.items():
        if len(by_player) < min_players:
            continue
        site = SITE_AREAS[group]
        if not any(site in areas for areas in by_player.values()):
            continue
        hits.append(
            AnomalyHit(
                rule=STACK,
                area=site,
                sample_t_s=seconds,
                players=len(by_player),
                alive=len(alive[seconds]),
                site=group,
            )
        )
    return sorted(hits, key=lambda hit: (hit.sample_t_s, hit.area))


# -- Sisäinen -----------------------------------------------------------------


def _cell_median(
    points: Sequence[tuple[float, float, float]],
) -> tuple[float, float, float]:
    """Akselikohtainen mediaani ruutujoukosta.

    **Jokainen ruutu painaa yhden.** Mediaani lasketaan akseleittain eikä
    monidimensioisena (geometrisena) mediaanina: jälkimmäinen olisi
    iteratiivinen approksimaatio, jonka tulos riippuisi aloitusarvosta ja
    kierrosmäärästä -- eli sama demo voisi antaa eri keskipisteen eri ajolla.
    """
    return (
        median(p[0] for p in points),
        median(p[1] for p in points),
        median(p[2] for p in points),
    )


def _spread(
    points: Sequence[tuple[float, float, float]],
    centre: tuple[float, float, float],
) -> float:
    """Alueen koko: ruutujen **mediaanietäisyys** keskipisteestä.

    Mediaani eikä keskiarvo tai suurin. Yksi vanhentunut aluenimi kartan
    toisella laidalla venyttää molemmat jälkimmäiset, ja juuri se vika tekisi
    erottuvuusvartijasta epäluotettavan: mitattuna keskiarvo antaa
    ``Ancient_vs_kaljukostaja``lle suhteen 3,14 kun kahdelle muulle
    Ancient-demolle 3,88-3,92, mediaani 3,70 kun 3,82-3,95.
    """
    return median(math.dist(point, centre) for point in points)


def _is_ct_time_row(row: AreaPresence) -> bool:
    """Kelpaako rivi poikkeamasäännölle lainkaan.

    Kolme ehtoa yhdessä paikassa, koska kaikki kolme sääntöä tarvitsevat ne
    kaikki: elossa oleva **CT**-pelaaja **aikanäytepisteellä**. Ensikontaktin
    rivi ei kelpaa, koska sen ``sample_t_s`` on mitattu hetki -- se läpäisisi
    aikarajan sen mukaan, milloin kierroksella satuttiin ampumaan.
    """
    return (
        row.is_alive
        and row.side == RULE_SIDE
        and row.sample_kind == TIME_SAMPLE
    )


def _players_by_point(
    presences: Iterable[AreaPresence],
    t_areas: Mapping[str, AreaObservations],
    max_sample_s: float,
) -> dict[tuple[float, str], set[str]]:
    """``(näytepiste, alue) -> eri pelaajat``, vain T:n alueilta ja ajoissa.

    Pelaajat ovat **joukko**: kaksoisrivi samasta pelaajasta ei saa nostaa
    pelaajamäärää, koska juuri pelaajamäärä on raportin luku.
    """
    found: dict[tuple[float, str], set[str]] = {}
    for row in presences:
        if not _is_ct_time_row(row):
            continue
        area = normalize_area(row.area)
        if area is None or area not in t_areas or row.sample_t_s > max_sample_s:
            continue
        found.setdefault((row.sample_t_s, area), set()).add(row.player_id)
    return found


def _previous_areas(
    rows: Sequence[AreaPresence],
) -> dict[tuple[str, float], str]:
    """``(pelaaja, näytepiste) -> alue edellisellä näytepisteellä``.

    Vain nimetyt alueet: ``None`` ei ole suunta. Puuttuva avain tarkoittaa
    siis kahta asiaa yhtä aikaa -- kierroksen ensimmäinen näytepiste tai
    tuntematon edellinen alue -- ja kumpikin on sama vastaus: pelaaja ei
    saapunut mistään.

    **Näytepiste tiivistetään ensin, vasta sitten pariutetaan.** Ilman sitä
    kaksoisrivi samasta pelaajasta samalla näytepisteellä pariutuisi
    itsensä kanssa, jolloin lähtöalueeksi tulisi kohdealue -- ja
    ``source == area`` vaientaisi saapumisen kokonaan. Kaksoisrivi ei ole
    teoreettinen: sama vartija on jo :func:`_players_by_point`issa, jossa
    pelaajat ovat joukko. Jos sama pelaaja on kahdella eri alueella samalla
    näytepisteellä, taulu on ristiriitainen; silloin valitaan
    aakkosjärjestyksessä ensimmäinen, jotta tulos on sama ajosta toiseen.
    """
    # (pelaaja, näytepiste) -> alueet joukkona. Joukko tiivistää kaksoisrivin
    # yhdeksi havainnoksi ennen pariutusta.
    by_point: dict[tuple[str, float], set[str]] = {}
    for row in rows:
        area = normalize_area(row.area)
        seen = by_point.setdefault((row.player_id, row.sample_t_s), set())
        if area is not None:
            seen.add(area)

    seconds_by_player: dict[str, list[float]] = {}
    for player, seconds in by_point:
        seconds_by_player.setdefault(player, []).append(seconds)

    previous: dict[tuple[str, float], str] = {}
    for player, seconds_list in seconds_by_player.items():
        ordered = sorted(seconds_list)
        for earlier, later in zip(ordered, ordered[1:]):
            areas = by_point[(player, earlier)]
            if areas:
                previous[(player, later)] = min(areas)
    return previous


def _first_matching(
    events: Iterable[DamageEvent],
    bounds: RoundBounds,
    excluded: Collection[str],
) -> int | None:
    """Pienin tick, jolla tapahtuma täyttää kontaktin ehdot."""
    ticks = [e.tick for e in events if _is_contact(e, bounds, excluded)]
    return min(ticks) if ticks else None


def _is_contact(
    event: DamageEvent, bounds: RoundBounds, excluded: Collection[str]
) -> bool:
    """Onko tapahtuma ristiinpuolinen, aseellinen kontakti tällä kierroksella."""
    if bounds.freeze_end_tick is None or bounds.end_tick is None:
        return False
    if not bounds.freeze_end_tick <= event.tick <= bounds.end_tick:
        return False
    if event.attacker_id is None or event.victim_id is None:
        # Maailman aiheuttama vahinko: putoaminen tai istuttajaton pommi.
        return False
    if event.attacker_id == event.victim_id:
        return False
    if event.attacker_side not in SIDES or event.victim_side not in SIDES:
        return False
    if event.attacker_side == event.victim_side:
        # Oma vahinko. Se ei kerro vastustajan asetelmasta mitään.
        return False
    weapon_name = normalize_weapon(event.weapon)
    if weapon_name is None:
        # Tuntematon tai tyhjä asenimi ei kelpaa kontaktiksi. Tyhjä nimi ei ole
        # poissuljettujen listalla, joten se menisi muuten läpi juuri kuin
        # kiväärillä ammuttu osuma -- ja ensikontakti voisi aikaistua hetkeen,
        # jonka lähdettä ei tunneta.
        return False
    return weapon_name not in excluded


def _check_tick_rate(tick_rate: float) -> None:
    if not tick_rate > 0:
        raise ValueError(
            f"Tickrate {tick_rate!r} ei kelpaa näytteistykseen: sen on oltava "
            "positiivinen, muuten sekunteja ei voi muuntaa tickeiksi."
        )


def _unique_sorted_seconds(sample_seconds: Sequence[float]) -> list[float]:
    """Näytepisteet nousevassa järjestyksessä, kaksoiskappaleet poistettuna."""
    values = [float(s) for s in sample_seconds]
    negative = [s for s in values if s < 0]
    if negative:
        raise ValueError(
            f"Näytepiste {negative[0]:g} s on negatiivinen. Näytepisteet "
            "mitataan freezetimen lopusta eteenpäin, joten negatiivinen arvo "
            "osoittaisi ostoaikaan, jossa pelaajat eivät ole vielä liikkuneet."
        )
    return sorted(set(values))
