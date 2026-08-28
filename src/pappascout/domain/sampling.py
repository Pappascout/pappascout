"""Näytepisteiden valinta ja ensikontaktin tunnistus (AD-5, AD-10).

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

Moduuli on puhdas: ei tiedostoja, ei demoparser2:ta, ei asetuksia. Sen voi
siksi testata käsin rakennetuilla tietueilla, ja jokainen I/O-matriisin rivi on
täällä yhden funktiokutsun päässä.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from pappascout.constants import SIDES

__all__ = [
    "RoundBounds",
    "SamplePoint",
    "DamageEvent",
    "TIME_SAMPLE",
    "FIRST_CONTACT_SAMPLE",
    "normalize_weapon",
    "seconds_since_freeze_end",
    "sample_ticks",
    "first_contact_tick",
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
    sekunnit = _unique_sorted_seconds(sample_seconds)

    points: list[SamplePoint] = []
    for bounds in segments:
        if not bounds.is_samplable:
            continue
        freeze_end = bounds.freeze_end_tick
        end = bounds.end_tick
        assert freeze_end is not None and end is not None  # is_samplable
        for seconds in sekunnit:
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
    kielletyt = {
        w
        for w in (normalize_weapon(name) for name in exclude_weapons)
        if w is not None
    }

    osuma = _first_matching(hurt_events, round_bounds, kielletyt)
    if osuma is not None:
        return osuma
    if not fallback_death:
        return None
    return _first_matching(death_events, round_bounds, kielletyt)


# -- Sisäinen -----------------------------------------------------------------


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
    ase = normalize_weapon(event.weapon)
    if ase is None:
        # Tuntematon tai tyhjä asenimi ei kelpaa kontaktiksi. Tyhjä nimi ei ole
        # poissuljettujen listalla, joten se menisi muuten läpi juuri kuin
        # kiväärillä ammuttu osuma -- ja ensikontakti voisi aikaistua hetkeen,
        # jonka lähdettä ei tunneta.
        return False
    return ase not in excluded


def _check_tick_rate(tick_rate: float) -> None:
    if not tick_rate > 0:
        raise ValueError(
            f"Tickrate {tick_rate!r} ei kelpaa näytteistykseen: sen on oltava "
            "positiivinen, muuten sekunteja ei voi muuntaa tickeiksi."
        )


def _unique_sorted_seconds(sample_seconds: Sequence[float]) -> list[float]:
    """Näytepisteet nousevassa järjestyksessä, kaksoiskappaleet poistettuna."""
    arvot = [float(s) for s in sample_seconds]
    negatiiviset = [s for s in arvot if s < 0]
    if negatiiviset:
        raise ValueError(
            f"Näytepiste {negatiiviset[0]:g} s on negatiivinen. Näytepisteet "
            "mitataan freezetimen lopusta eteenpäin, joten negatiivinen arvo "
            "osoittaisi ostoaikaan, jossa pelaajat eivät ole vielä liikkuneet."
        )
    return sorted(set(arvot))
