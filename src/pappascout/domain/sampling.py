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

Poikkeamasäännöt lukevat alueen puoliorientaation
-------------------------------------------------
Story 2.5 lisää kaksi sääntöä: :func:`ct_advance_hits` ja :func:`crunch_hits`.
Molemmat vastaavat samaan kysymykseen -- **onko subjektin CT-pelaaja
alueella, joka on siinä demossa T:n hallussa** -- ja jakavat siksi saman
orientaatiolaskennan (:func:`t_side_shares`): kahdella laskennalla ne voisivat
olla eri mieltä siitä, kumman aluetta alue on.

**Kumpikaan sääntö ei sisällä toista.** Crunch lisää orientaatioehtoon
suuntavaatimuksen mutta **pudottaa kierrostyyppirajauksen**, joten
osumajoukot leikkaavat toisiaan: säästökierroksella crunch tuottaa myös
etenemisosuman, täydellä ostolla vain crunchin (mitattu: MatureMayhem Anubis
k10). Kumpaakaan ei siis saa kuvata toisen "tiukempana muotona".

Orientaatio **tulee argumenttina** eikä lasketa täällä. Se on demon oma
havainto alueen elossa-havainnoista aikanäytepisteillä, ja se on laskettava
**suodattamattomasta** näytepistetaulusta eli molempien joukkueiden riveistä.
Tämä on mitattu ehto eikä mieltymys: subjektin omilla riveillä laskettuna
jokainen tosi positiivinen katoaa, koska poikkeama syö oman havaitsemisensa
(:class:`AreaObservations`).

Tyhjä tulos on **kelvollinen tulos** eikä puute: demo, jossa ei ole
poikkeamia, on havainto siitä ettei poikkeamia ollut.

Moduuli on puhdas: ei tiedostoja, ei demoparser2:ta, ei asetuksia. Sen voi
siksi testata käsin rakennetuilla tietueilla, ja jokainen I/O-matriisin rivi on
täällä yhden funktiokutsun päässä.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from pappascout.constants import SAVING_ROUND_TYPES, SIDES

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
    "AreaObservations",
    "AreaPresence",
    "AnomalyHit",
    "t_side_shares",
    "ct_advance_hits",
    "crunch_hits",
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
#: Molemmat säännöt kysyvät, mitä **subjekti tekee CT:nä** vastustajan
#: alueella, joten T-puolen rivit eivät voi tuottaa osumaa kummallakaan.
#: Vakiona siksi, että sama arvo tarvitaan kahdessa paikassa: rivien
#: suodatuksessa (:func:`_is_ct_time_row`) ja aggregoinnin kattavuusluvussa,
#: joka kertoo montako kierrosta sääntö **voi** osua. Kahtena kirjoitettuna
#: kattavuus voisi luvata enemmän kuin sääntö tutkii.
RULE_SIDE = "CT"

#: Crunch: sama alue, mutta vähintään kaksi pelaajaa **saapuneena** vähintään
#: kahdesta eri suunnasta yhtä aikaa -- **millä tahansa kierrostyypillä**.
#: Sama orientaatioehto kuin etenemisellä, yksi lisävaatimus ja yksi rajaus
#: vähemmän, joten sääntöjen osumajoukot leikkaavat toisiaan eikä kumpikaan
#: sisällä toista.
CRUNCH = "crunch"


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

    Attributes:
        rule: :data:`CT_ADVANCE` tai :data:`CRUNCH`.
        area: Alue, jolla osuma havaittiin. Ei koskaan ``None``: alue ilman
            nimeä ei voi olla T:n aluetta.
        sample_t_s: Näytepiste, jolla osuma havaittiin.
        players: Eri pelaajien määrä. Etenemisessä kaikki alueella olevat
            CT-pelaajat, crunchissa vain **saapuneet** (ks.
            :func:`crunch_hits`).
        t_share: Alueen T-osuus tässä demossa.
        observations: Alueen havaintojen määrä, eli orientaation oma otanta.
        sources: Crunchin lähtöalueet aakkosjärjestyksessä; etenemisellä
            tyhjä.
    """

    rule: str
    area: str
    sample_t_s: float
    players: int
    t_share: float
    observations: int
    sources: tuple[str, ...] = ()


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


# -- Sisäinen -----------------------------------------------------------------


def _is_ct_time_row(row: AreaPresence) -> bool:
    """Kelpaako rivi poikkeamasäännölle lainkaan.

    Kolme ehtoa yhdessä paikassa, koska molemmat säännöt tarvitsevat kaikki
    kolme: elossa oleva **CT**-pelaaja **aikanäytepisteellä**. Ensikontaktin
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
