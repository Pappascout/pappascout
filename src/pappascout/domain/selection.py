"""Otteluvalinta rosterikynnyksellä (Story 3.3).

Moduuli on **puhdas**: se ei tunne tiedostoja, HTTP:tä eikä FACEITin sanastoa.
Sisään tulee :class:`MapCandidate` -- yksi kartta yhdessä ottelussa, kaksi
pelaajajoukkoa ja tieto siitä, onko ottelu liigasta -- ja ulos tulee
:class:`MapSelection`, joka on valintatiedoston rivi. Muunnoksen indeksien
sanastosta tekee ``stages.select``, joten tämän moduulin säännöt ovat
testattavissa käsin rakennetuilla joukoilla ilman verkkoa ja ilman arkistoa.

Viisi sääntöä, jotka tämä moduuli pitää voimassa
------------------------------------------------

**Kynnys on joukko-operaatio, ei uusi käsite.** Vakirosteri on SteamID64-joukko
(``domain.teams.Team.player_ids``), kartan kokoonpano on toinen SteamID64-joukko,
ja päätös on niiden leikkauksen koko. Nimimerkit ovat vain syytä varten;
yksikään päätös ei riipu niistä.

**Rosteriluokka on ennuste ennen parsintaa ja havainto sen jälkeen.** FACEITin
``roster`` on **ottelukohtainen**, ei karttakohtainen, mutta Pappaliiga sallii
kaksi vaihtoa karttojen välissä -- kartan todellisen kokoonpanon näkee vasta
demosta. Rivi sanoo kummasta on kyse (:data:`ROSTER_SOURCES`), täsmälleen kuten
``map_name_source`` Story 2.11:ssä. Kun molemmat ovat tiedossa, **havainto
voittaa** ja ero ottelurosteriin kerrotaan -- vaihto karttojen välissä on juuri
se asia, jota varten kynnys arvioidaan karttakohtaisesti, eikä sitä saa vaientaa.

**Neljä vakipelaajaa ja yksi ulkopuolinen kelpaa.** Veeti 2026-09-04: *"ottelu on
samaa joukkuetta vastaan vaikka toisessa ottelussa heillä olisi yksi
substitution pelaaja."* Ulkopuolisen sijainnit lasketaan mukaan; luokka erottelee
``5/5`` ja ``4/5``, jotta raportti voi erotella ne kierrokset toisistaan.

**Vetotiedon kartta ei ole todiste pelatusta kartasta.** Kolmen kartan ottelusta
osa päättyy kahteen, ja vedossa on silti kolme nimeä. Kartta, joka **saattoi
jäädä pelaamatta**, saa rivin muttei pääse otantaan -- ja rivi sanoo syyksi
juuri sen (:func:`guaranteed_maps`). Parsittu demo on todiste: sellaista ei ole
olemassa kartasta, jota ei pelattu, joten havainto palauttaa varmuuden.

**Hylkäyksellä on aina luettava syy.** :attr:`MapSelection.roster_reason` ei ole
koskaan tyhjä -- ei hyväksytyllä eikä hylätyllä rivillä. Käyttäjä ei koodaa itse,
joten "tämä kartta ei kelvannut" ilman lukuja olisi päätös, jota hän ei voi
tarkistaa. Syy kertoo montako löytyi, mikä kynnys on, ketkä olivat ulkopuolisia
ja **mistä tieto on peräisin**.

Mitä tämä moduuli **ei** tee
----------------------------
Se ei päättele, onko ottelu pelattu: pelaamattomasta ottelusta ei rakenneta
:class:`MapCandidate`ia lainkaan (mitattu 2026-09-04, ``map_picks`` on tyhjä
60/66 ottelussa -- ajastettu ottelu ei ole "valinta odottaa" vaan "ei vielä
olemassa"). Se ei myöskään päättele ``is_league``ia: se on ``competition_id``:n
ja ``[league].championship_ids``-listan vertailu, ja vertailun tekee vaihe, joka
näkee asetukset.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Final, Literal

from pappascout.constants import ROSTER_CLASSES, RosterClass
from pappascout.errors import SettingsError

__all__ = [
    "ROSTER_SOURCES",
    "RosterSource",
    "ROSTER_SOURCE_FI",
    "map_demo_id",
    "guaranteed_maps",
    "class_labels",
    "MapCandidate",
    "MapSelection",
    "evaluate",
    "select_maps",
    "sort_key",
    "counts",
]

#: Mistä kartan kokoonpano tiedetään.
#:
#: ``observed``
#:     Demon kokoonpanotaulusta (``parsed/<map_demo_id>/lineups.parquet``).
#:     Tämä on se, ketkä kartalla **olivat**.
#: ``predicted``
#:     Ottelun rosterista. Tämä on se, ketkä kartalla **odotettiin olevan** --
#:     FACEITin rosteri on ottelukohtainen, ja karttojen välissä saa vaihtaa.
#:
#: Järjestys on ensisijaisuusjärjestys: havainto ennen ennustetta.
ROSTER_SOURCES: Final[tuple[str, ...]] = ("observed", "predicted")
RosterSource = Literal["observed", "predicted"]

#: Lähteen suomenkielinen nimi käyttäjän tulostetta ja syytä varten.
ROSTER_SOURCE_FI: Final[dict[str, str]] = {
    "observed": "havainto",
    "predicted": "ennuste",
}


def map_demo_id(match: str, index: int) -> str:
    """Yksikön tunniste: ``{match_id}-{map_index}`` (AD-7).

    ``map_index`` on **0-pohjainen indeksi** ottelun karttalistaan, ja juuri
    tässä se ensimmäisen kerran kirjoitetaan tunnisteeksi -- ``parse`` ja
    ``classify`` saavat tunnisteen valmiina. Käyttäjälle näytettävä ``map_no``
    on ``map_index + 1``, eikä sitä koskaan kirjoiteta tunnisteeseen.

    >>> map_demo_id("1-f6a06dc8", 0)
    '1-f6a06dc8-0'

    Raises:
        ValueError: Jos ``index`` on negatiivinen tai ``match`` on tyhjä.
            Kumpikaan ei voi tuottaa tunnistetta, jolla mitään löytyisi, ja
            hiljainen ``"-1"``-pääte osoittaisi tiedostoon, jota ei ole.
    """
    if not match:
        raise ValueError("map_demo_id tarvitsee ottelutunnisteen; se oli tyhjä.")
    if index < 0:
        raise ValueError(
            f"map_index ei voi olla negatiivinen, oli {index}. "
            "Indeksi on 0-pohjainen paikka ottelun karttalistassa."
        )
    return f"{match}-{index}"


def guaranteed_maps(best_of: int | None) -> int | None:
    """Montako karttaa ``best_of``-ottelussa pelataan **varmasti**.

    Ottelu päättyy, kun toinen on voittanut ``best_of // 2 + 1`` karttaa, joten
    vähintään niin monta karttaa pelataan aina -- ja loput vain, jos ottelu ei
    ole vielä ratkennut.

    >>> [guaranteed_maps(n) for n in (1, 2, 3, 4, 5)]
    [1, 2, 2, 3, 3]

    **BO2 on kokonaan varma, ja se on kaavan tulos eikä poikkeus:** kahdesta
    kartasta ei voi voittaa kahta ennen kuin molemmat on pelattu. Pappaliigan
    runkosarjassa (mitattu 2026-09-04: ``best_of`` on ``2`` kaikissa 66
    ottelussa) yksikään rivi ei siis jää epävarmaksi. BO3-playoffeissa kolmas
    kartta jää -- ja juuri siksi tämä sääntö on olemassa.

    Args:
        best_of: Ottelun pituus karttoina, tai ``None``.

    Returns:
        Varmasti pelattujen karttojen määrä, tai ``None`` jos ottelun pituutta
        ei tiedetä. ``None`` on eri asia kuin nolla: se tarkoittaa, ettei
        varmuudesta voi sanoa mitään suuntaan eikä toiseen.
    """
    if best_of is None or best_of < 1:
        return None
    return best_of // 2 + 1


def class_labels(roster_size: int, roster_min_regulars: int) -> tuple[str, str]:
    """Rosteriluokkien nimet asetusarvoista: ``("5/5", "4/5")``.

    Nimet **johdetaan kynnyksistä** eikä kirjoiteta käsin, jotta luokka ei voi
    valehdella asetuksesta. Samalla tämä on se kohta, joka sitoo
    ``[thresholds]``-arvot :data:`~pappascout.constants.ROSTER_CLASSES`-
    luetteloon: jos kynnyksiä muutetaan, luokan nimi lakkaa kelpaamasta
    ``CLASSIFIED``-skeeman enumiin, ja se on parempi kuulla tässä kuin
    kolme vaihetta myöhemmin Polarsin tyyppivirheenä.

    >>> class_labels(5, 4)
    ('5/5', '4/5')

    Args:
        roster_size: Montako pelaajaa kartalla on. ``[thresholds].roster_size``.
        roster_min_regulars: Montako heistä on oltava vakirosterista.
            ``[thresholds].roster_min_regulars``.

    Returns:
        ``(täysi, vajaa)`` -- luokka, kun kaikki ovat vakirosterista, ja luokka,
        kun kynnys täyttyy mutta yksi on ulkopuolinen.

    Raises:
        ~pappascout.errors.SettingsError: Jos kynnykset ovat epäkelvot tai jos
            niistä johdettu nimi ei ole ``ROSTER_CLASSES``-luettelossa.
            **Asetusvirhe eikä ohjelmavirhe**: molemmat arvot tulevat
            ``settings.toml``ista, ja kumpikin on itsessään kelvollinen
            ``PositiveInt``. Paljas ``ValueError`` päätyisi komentorivillä
            muotoon "Odottamaton virhe -- ohjelmavirhe", vaikka korjaus on
            käyttäjän asetustiedostossa.
    """
    if roster_size < 1:
        raise SettingsError(
            f"Asetus [thresholds].roster_size on {roster_size}, mutta kartalla "
            "on aina vähintään yksi pelaaja. Korjaa arvo settings.tomlissa."
        )
    if not 1 <= roster_min_regulars <= roster_size:
        raise SettingsError(
            f"Asetus [thresholds].roster_min_regulars on {roster_min_regulars}, "
            f"mutta sen on oltava välillä 1..{roster_size} "
            "([thresholds].roster_size). Korjaa arvo settings.tomlissa."
        )
    full = f"{roster_size}/{roster_size}"
    partial = f"{roster_min_regulars}/{roster_size}"
    unknown = [name for name in (full, partial) if name not in ROSTER_CLASSES]
    if unknown:
        raise SettingsError(
            f"Kynnyksistä roster_size={roster_size} ja "
            f"roster_min_regulars={roster_min_regulars} johdettu rosteriluokka "
            f"{', '.join(unknown)} ei ole tunnettujen luokkien joukossa "
            f"({', '.join(ROSTER_CLASSES)}).\n"
            "Rosteriluokka on myös classified-taulun enum-arvo, joten sitä ei "
            "voi keksiä ajossa. Palauta kynnykset settings.tomlissa arvoihin, "
            "joista syntyy tunnettu luokka."
        )
    return full, partial


@dataclass(frozen=True)
class MapCandidate:
    """Yksi MapDemo ennen päättelyä. Moduulin syöte.

    Attributes:
        map_demo_id: Yksikön tunniste, ``{match_id}-{map_index}``.
        match_id: Ottelu, jonka kartta tämä on.
        map_index: 0-pohjainen paikka ottelun karttalistassa.
        map_name: Kartan nimi vetotiedosta, tai ``None``. **Ei koskaan päätöksen
            peruste** -- se on rivin luettavuutta varten. Kartan lopullinen nimi
            luetaan demon otsikosta (Story 2.11), ja tämä on vetotiedon havainto.
        is_league: Onko ottelu ``[league].championship_ids``-listalta. Vertailun
            tekee vaihe; tämä on sen tulos.
        certainly_played: Onko kartta varmasti pelattu. ``False`` tarkoittaa,
            että kartta on vetotiedossa mutta ottelu on voinut ratketa ennen
            sitä (ks. :func:`guaranteed_maps`). Vaihe laskee tämän
            ``best_of``ista; ``True`` on oletus, koska ilman ottelun pituutta
            ei ole perustetta epäillä yhtään karttaa.
        match_roster: Ottelun rosteri -- **ennuste** siitä, ketkä kartalla
            olivat. FACEITin ``roster`` eli aloittajat, ei vaihtopelaajat:
            vaihtopenkki ei ole kartalla, ja sen laskeminen mukaan ennustaisi
            kymmenen pelaajaa viiden paikalle.
        observed_players: Kartan kokoonpano demosta, tai ``None`` jos demoa ei
            ole parsittu. ``None`` ja tyhjä joukko ovat **eri asioita**:
            edellinen on "ei tiedossa", jälkimmäinen "demo luettiin eikä siinä
            ollut tätä joukkuetta".
        observation_note: Suomenkielinen selitys sille, **miksi havaintoa ei
            ole**, tai ``None``. Rikkinäinen kokoonpanotaulu ja kahden
            kokoonpanon tasapeli ovat molemmat "ei havaintoa", mutta kumpikaan
            ei ole sama asia kuin "demoa ei ole parsittu" -- ja ilman tätä
            kenttää ero katoaisi jäljettömästi. Kulkee rivin syyhyn.
    """

    map_demo_id: str
    match_id: str
    map_index: int
    map_name: str | None = None
    is_league: bool = False
    certainly_played: bool = True
    match_roster: frozenset[str] = frozenset()
    observed_players: frozenset[str] | None = None
    observation_note: str | None = None


@dataclass(frozen=True)
class MapSelection:
    """Yhden MapDemon valintapäätös. Valintatiedoston rivi.

    Attributes:
        map_demo_id: Yksikön tunniste.
        match_id: Ottelu.
        map_index: Kartan paikka ottelussa.
        map_name: Kartan nimi vetotiedosta, tai ``None``.
        is_league: Onko ottelu liigasta.
        roster_ok: Kelpaako kartta otantaan.
        roster_reason: **Aina luettava syy**, myös hyväksytyllä rivillä. Kertoo
            luvut ja kynnyksen, jotta päätöksen voi tarkistaa avaamatta demoa.
        roster_class: ``"5/5"`` tai ``"4/5"``, tai ``None`` jos kartta ei
            kelvannut. Hylätyllä rivillä luokka olisi väite kierroksista, joita
            ei lasketa; hyväksytyllä rivillä sen puuttuminen tekisi rivistä
            otannan, jota kumpikaan luokkalaskuri ei löydä.
        roster_source: ``"observed"`` tai ``"predicted"``; ks.
            :data:`ROSTER_SOURCES`.
        certainly_played: Tiedetäänkö, että kartta pelattiin. **Johtopäätös
            eikä syöte**: tosi, kun ottelun pituus takaa kartan
            (:func:`guaranteed_maps`) **tai** kun demo on parsittu -- sellaista
            ei ole olemassa kartasta, jota ei pelattu. Epätosi rivi on
            vetotiedossa mutta odottaa todistetta.
        regulars: Kartan pelaajat, jotka ovat vakirosterissa. Lajiteltu.
        outsiders: Kartan pelaajat, jotka eivät ole vakirosterissa. Lajiteltu.
            **Nämä lasketaan mukaan** otantaan, kun kynnys täyttyy -- ero on
            luokassa, ei siinä kuka on mukana.
        players_seen: Montako pelaajaa kartalla tiedettiin olevan.
        joined: Pelaajat, jotka olivat demossa muttei ottelurosterissa.
            Tyhjä, kun havaintoa tai ennustetta ei ole vertailtavaksi.
        left: Pelaajat, jotka olivat ottelurosterissa muttei demossa.
    """

    map_demo_id: str
    match_id: str
    map_index: int
    map_name: str | None
    is_league: bool
    roster_ok: bool
    roster_reason: str
    roster_class: RosterClass | None
    roster_source: RosterSource
    certainly_played: bool = True
    regulars: tuple[str, ...] = ()
    outsiders: tuple[str, ...] = ()
    players_seen: int = 0
    joined: tuple[str, ...] = ()
    left: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.roster_reason.strip():
            raise ValueError(
                f"Valintarivillä {self.map_demo_id!r} ei ole syytä. "
                "Päätös ilman syytä ei ole tarkistettavissa, eikä sellaista "
                "riviä saa kirjoittaa."
            )
        if self.roster_source not in ROSTER_SOURCES:
            raise ValueError(
                f"Valintarivin {self.map_demo_id!r} lähde on "
                f"{self.roster_source!r}, jota ei ole tunnettujen lähteiden "
                f"joukossa ({', '.join(ROSTER_SOURCES)})."
            )
        if not self.roster_ok and self.roster_class is not None:
            raise ValueError(
                f"Hylätyllä valintarivillä {self.map_demo_id!r} on rosteriluokka "
                f"{self.roster_class!r}. Luokka on väite kierroksista, joita ei "
                "lasketa."
            )
        if self.roster_ok and self.roster_class is None:
            raise ValueError(
                f"Hyväksytyllä valintarivillä {self.map_demo_id!r} ei ole "
                "rosteriluokkaa. Sellainen rivi olisi otannassa muttei "
                "kummassakaan luokkalaskurissa, eikä mikään kertoisi eroa."
            )

    @property
    def source_fi(self) -> str:
        """Lähde suomeksi: ``havainto`` tai ``ennuste``."""
        return ROSTER_SOURCE_FI[self.roster_source]

    @property
    def drifted(self) -> bool:
        """Erosiko kartan kokoonpano ottelurosterista?"""
        return bool(self.joined or self.left)


def evaluate(
    candidate: MapCandidate,
    *,
    roster: Set[str],
    roster_size: int,
    roster_min_regulars: int,
    names: Mapping[str, str] | None = None,
) -> MapSelection:
    """Päätä yhden MapDemon kohtalo joukko-operaationa.

    Tarkistukset ovat tässä järjestyksessä, ja järjestys on merkityksellinen:
    jokainen niistä tekee seuraavasta merkityksettömän, joten ensimmäinen
    osuva on aina se, joka kertoo eniten.

    1. **Vakirosteri tuntematon** -- ilman sitä ei ole mitään, mitä vasten
       verrata, eikä kynnyksen alitus ole tosi väite.
    2. **Kartta saattoi jäädä pelaamatta** -- kokoonpanon arviointi kartalle,
       jota ei ehkä ole olemassa, olisi väite tyhjästä. Parsittu demo kumoaa
       tämän: sitä ei olisi olemassa pelaamattomasta kartasta.
    3. **Kokoonpano tuntematon** -- eri syy riippuen siitä, oliko demo
       parsittu vai ei.
    4. **Kynnys.**

    Args:
        candidate: Kartta ja sen kaksi pelaajajoukkoa.
        roster: Joukkueen vakirosteri SteamID64-joukkona
            (``domain.teams.Team.player_ids``).
        roster_size: ``[thresholds].roster_size`` -- montako pelaajaa kartalla on.
        roster_min_regulars: ``[thresholds].roster_min_regulars`` -- montako
            heistä on oltava vakirosterista.
        names: SteamID64 -> nimimerkki, pelkkää syytä varten. Puuttuva nimi on
            tunniste sellaisenaan: syy on ihmiselle, mutta keksitty nimi
            osoittaisi väärään pelaajaan.

    Returns:
        :class:`MapSelection`, jolla on aina syy.

    Raises:
        ~pappascout.errors.SettingsError: Jos kynnykset ovat epäkelvot; ks.
            :func:`class_labels`.
    """
    full_label, partial_label = class_labels(roster_size, roster_min_regulars)
    show = _namer(names)

    observed = candidate.observed_players
    if observed is not None:
        source: RosterSource = "observed"
        on_map = frozenset(observed)
    else:
        source = "predicted"
        on_map = frozenset(candidate.match_roster)

    joined, left = _drift(candidate)
    note = candidate.observation_note

    # Rivin ``certainly_played`` on **johtopaatos**, ei syote: ottelun pituus
    # takaa kartan, TAI parsittu demo todistaa sen. Pelkka syotteen kopiointi
    # jattaisi rivin ikuisesti epavarmaksi, vaikka demo on arkistossa -- ja
    # yhteenveto kertoisi epavarmoja karttoja, jotka on jo todistettu.
    known_played = candidate.certainly_played or observed is not None

    def rejected(reason: str) -> MapSelection:
        return _row(
            candidate,
            certainly_played=known_played,
            roster_ok=False,
            roster_class=None,
            roster_source=source,
            roster_reason=_with_note(reason, note),
            regulars=(),
            outsiders=(),
            players_seen=len(on_map),
            joined=joined,
            left=left,
        )

    if not roster:
        return rejected(
            "Ei kelpaa: joukkueen vakirosteria ei tiedetä, joten kynnystä ei "
            "voi arvioida lainkaan."
        )

    # Havainto on todiste pelatusta kartasta: parsittua demoa ei ole olemassa
    # kartasta, jota ei pelattu.
    if not candidate.certainly_played and observed is None:
        return rejected(
            f"Ei kelpaa: kartta on vetotiedossa {candidate.map_index + 1}. "
            "eikä ottelun pituus takaa, että se pelattiin. Kartta pääsee "
            "otantaan, kun sen demo on ladattu ja parsittu -- demo on todiste."
        )

    if not on_map:
        return rejected(_no_players_reason(source))

    regulars = tuple(sorted(on_map & frozenset(roster)))
    outsiders = tuple(sorted(on_map - frozenset(roster)))
    found = len(regulars)

    # Luokka kertoo, montako **kartan viidestä paikasta** on vakipelaajan.
    # Ilman kattoa kuuden pelaajan kokoonpano tuottaisi luokan 5/5 ja syyn
    # "6/6", eli luokka ja syy väittäisivät eri asiaa.
    counted = min(found, roster_size)
    if counted >= roster_size:
        label: str | None = full_label
        ok = True
    elif counted >= roster_min_regulars:
        label = partial_label
        ok = True
    else:
        label = None
        ok = False

    reason = _reason(
        ok=ok,
        label=label,
        found=found,
        on_map=len(on_map),
        outsiders=outsiders,
        roster_min_regulars=roster_min_regulars,
        roster_size=roster_size,
        source=source,
        joined=joined,
        left=left,
        show=show,
        note=note,
    )
    return _row(
        candidate,
        certainly_played=known_played,
        roster_ok=ok,
        roster_class=label,  # type: ignore[arg-type]
        roster_source=source,
        roster_reason=reason,
        regulars=regulars,
        outsiders=outsiders,
        players_seen=len(on_map),
        joined=joined,
        left=left,
    )


def select_maps(
    candidates: Iterable[MapCandidate],
    *,
    roster: Set[str],
    roster_size: int,
    roster_min_regulars: int,
    names: Mapping[str, str] | None = None,
) -> tuple[MapSelection, ...]:
    """Päätä monen MapDemon kohtalo. Ks. :func:`evaluate`.

    Järjestys on sama kuin syötteessä: vaihe päättää järjestyksen, jotta kahden
    ajon ero on diffattavissa.
    """
    return tuple(
        evaluate(
            candidate,
            roster=roster,
            roster_size=roster_size,
            roster_min_regulars=roster_min_regulars,
            names=names,
        )
        for candidate in candidates
    )


# -- Sisäiset apurit ---------------------------------------------------------


def _row(
    candidate: MapCandidate,
    *,
    certainly_played: bool,
    roster_ok: bool,
    roster_class: RosterClass | None,
    roster_source: RosterSource,
    roster_reason: str,
    regulars: tuple[str, ...],
    outsiders: tuple[str, ...],
    players_seen: int,
    joined: tuple[str, ...],
    left: tuple[str, ...],
) -> MapSelection:
    return MapSelection(
        map_demo_id=candidate.map_demo_id,
        match_id=candidate.match_id,
        map_index=candidate.map_index,
        map_name=candidate.map_name,
        is_league=candidate.is_league,
        certainly_played=certainly_played,
        roster_ok=roster_ok,
        roster_reason=roster_reason,
        roster_class=roster_class,
        roster_source=roster_source,
        regulars=regulars,
        outsiders=outsiders,
        players_seen=players_seen,
        joined=joined,
        left=left,
    )


def _drift(candidate: MapCandidate) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Havainnon ja ennusteen ero -- vaihto karttojen välissä.

    Ero lasketaan **vain kun molemmat ovat olemassa ja epätyhjiä**. Ilman
    ottelurosteria ero olisi koko havaittu kokoonpano; ilman havaittua
    kokoonpanoa ero olisi koko ottelurosteri, ja rivi kertoisi "vaihdosta"
    kartalla, jonka oma syy sanoo kokoonpanon olevan tuntematon.
    """
    observed = candidate.observed_players
    if not observed or not candidate.match_roster:
        return (), ()
    predicted = frozenset(candidate.match_roster)
    return tuple(sorted(observed - predicted)), tuple(sorted(predicted - observed))


def _namer(names: Mapping[str, str] | None):
    known = names or {}

    def show(player: str) -> str:
        return known.get(player) or player

    return show


def _with_note(reason: str, note: str | None) -> str:
    return reason if note is None else f"{reason} {note}"


def _no_players_reason(source: RosterSource) -> str:
    if source == "observed":
        return (
            "Ei kelpaa: kartan kokoonpanoa ei tiedetä. Demo on parsittu, mutta "
            "siinä ei ollut yhtään tämän joukkueen pelaajaa."
        )
    return (
        "Ei kelpaa: kartan kokoonpanoa ei tiedetä. Ottelurosteri on tyhjä eikä "
        "demoa ole parsittu, joten kynnystä ei voi arvioida."
    )


def _reason(
    *,
    ok: bool,
    label: str | None,
    found: int,
    on_map: int,
    outsiders: tuple[str, ...],
    roster_min_regulars: int,
    roster_size: int,
    source: RosterSource,
    joined: tuple[str, ...],
    left: tuple[str, ...],
    show,
    note: str | None,
) -> str:
    """Yhden rivin syy: luvut, kynnys, ulkopuoliset, ero ja lähde.

    Sama runko sekä hyväksytylle että hylätylle riville, koska käyttäjä
    tarkistaa molemmat samasta paikasta. Ero on ensimmäisessä lauseessa.
    """
    origin = ROSTER_SOURCE_FI[source]
    origin_text = (
        "kokoonpano demosta" if source == "observed" else "kokoonpano ottelurosterista"
    )
    parts: list[str] = []

    if ok:
        parts.append(
            f"Kelpaa: {found}/{on_map} kartan pelaajasta on vakirosterissa, "
            f"luokka {label}."
        )
    else:
        parts.append(
            f"Ei kelpaa: vain {found}/{on_map} kartan pelaajasta on "
            f"vakirosterissa, ja kynnys on {roster_min_regulars}/{roster_size}."
        )

    # Luokka puhuu aina viidestä paikasta. Jos kartalla oli eri määrä pelaajia,
    # luokan nimittäjä ja lauseen nimittäjä eroavat -- ja se sanotaan, ettei
    # lukija päättelisi luokasta ulkopuolista, jota ei ole.
    if on_map != roster_size:
        parts.append(
            f"Huomaa: kokoonpanossa oli {on_map} pelaajaa odotetun "
            f"{roster_size} sijaan, joten luokka ja lukusuhde eivät ole "
            "samasta nimittäjästä."
        )

    if outsiders:
        named = ", ".join(show(player) for player in outsiders)
        parts.append(f"Vakirosterin ulkopuolelta: {named}.")
    elif ok and found < roster_size:
        parts.append(
            "Ulkopuolisia ei ollut -- vajaa luokka johtuu kokoonpanon koosta, "
            "ei vieraasta pelaajasta."
        )

    if joined or left:
        changed: list[str] = []
        if joined:
            changed.append("mukaan tuli " + ", ".join(show(p) for p in joined))
        if left:
            changed.append("pois jäi " + ", ".join(show(p) for p in left))
        parts.append("Kokoonpano eroaa ottelurosterista: " + "; ".join(changed) + ".")

    parts.append(f"Lähde: {origin} ({origin_text}).")
    return _with_note(" ".join(parts), note)


def sort_key(selection: MapSelection) -> tuple[str, int]:
    """Rivien järjestys tiedostossa: ottelu, sitten kartan indeksi."""
    return (selection.match_id, selection.map_index)


def counts(selections: Sequence[MapSelection]) -> dict[str, int]:
    """Yhteenvetoluvut valintariveistä, käyttäjän tulostetta varten.

    Luvut lasketaan **riveistä eikä ajon varrelta**, jotta tiedosto ja
    yhteenveto eivät voi kertoa eri asiaa. Kaksi invarianttia pitää:
    ``accepted + rejected == map_demos`` ja ``class_5/5 + class_4/5 ==
    accepted`` -- jälkimmäisen takaa :meth:`MapSelection.__post_init__`.
    """
    accepted = [row for row in selections if row.roster_ok]
    return {
        "map_demos": len(selections),
        "accepted": len(accepted),
        "rejected": len(selections) - len(accepted),
        "league": sum(1 for row in selections if row.is_league),
        "observed": sum(1 for row in selections if row.roster_source == "observed"),
        "predicted": sum(1 for row in selections if row.roster_source == "predicted"),
        "drifted": sum(1 for row in selections if row.drifted),
        "uncertain": sum(1 for row in selections if not row.certainly_played),
        **{
            f"class_{label}": sum(1 for row in accepted if row.roster_class == label)
            for label in ROSTER_CLASSES
        },
    }
