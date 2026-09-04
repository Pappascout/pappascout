"""Joukkueen identiteetti: nimihaku ja vakirosteri (Story 3.2).

Moduuli on **puhdas**: se ei tunne HTTP:tä, tiedostoja eikä FACEITin sanastoa.
Sisään tulee :class:`TeamObservation` -- yksi havainto joukkueesta yhdessä
ottelussa -- ja ulos tulee :class:`Team`. Muunnoksen lähteen sanastosta tekee
``stages.discover``, joten tämän moduulin säännöt ovat testattavissa käsin
rakennetuilla havainnoilla ilman verkkoa.

Neljä sääntöä, jotka tämä moduuli pitää voimassa
------------------------------------------------

**Identiteetti on rosteri, tunniste on vain tunniste.** Lähteen oma
joukkuetunniste (FACEITillä ``faction_id``) on *avain*, ei identiteetti: uusi
kausi tai uudelleenrekisteröinti antaa samalle porukalle uuden tunnisteen, ja
kierrätetty tunniste antaisi kahdelle eri porukalle saman. :func:`build_teams`
liittää siksi kaksi tunnistetta samaksi joukkueeksi, kun niiden rosterit
jakavat vähintään ``min_common`` pelaajaa -- sama kynnys
(``[thresholds].team_identity_min_common``) ja sama vertailutapa kuin
``domain.aggregate.lineups_of_same_team``illa. **Kanoninen ``team_key`` on
varhaisimman havainnon tunniste**, joten se ei muutu, kun uusi kausi tuo uuden
tunnisteen.

*Tätä sääntöä ei voi todentaa nykyistä live-aineistoa vasten*: asetuksissa on
yksi championship, ja mitattu tulos oli tasan yksi ``faction_id`` per joukkue.
Sääntö on siis yksikkötesteillä todennettu ja odottaa toista kautta -- se
sanotaan tässä ääneen, jottei lukija luulisi sitä mitatuksi.

**Rosteri on yhdiste, ei viimeisin ottelu.** Rosteri kootaan joukkueen
*kaikista* otteluista, ja siinä ovat sekä aloittajat että vaihtopelaajat.
Viimeisin ottelu kertoisi vain siitä illasta, ja pelkkä ``roster`` aliarvioisi
joukkueen järjestelmällisesti: mitattu 2026-09-04, ``Lindberq_`` on arkiston
demossa muttei kertaakaan Rcave Veteransin ``roster``issa.

**Yhdiste ei kuitenkaan ole ikuinen.** Kesken kauden siirtyvä pelaaja jäisi
pelkällä yhdisteellä molempiin joukkueisiin pysyvästi, ja se paisuttaisi
rostereita sekä vääristäisi rosterikynnystä (Story 3.3). Sääntö on siksi:
**pelaaja kuuluu siihen joukkueeseen, joka havaitsi hänet viimeksi**; aiemmat
joukkueet säilyttävät hänet :attr:`Team.released`issä, jottei havainto katoa.
Jos kaksi joukkuetta havaitsi hänet **yhtä myöhään** -- tai jos havaintojen
aika ei ole tiedossa -- häntä ei siirretä kummastakaan, vaan hän on molempien
:attr:`Team.shared_players`issä. Kiistaa ei ratkaista arpomalla.

**Monitulkintaisuus on tulos, ei poikkeus.** :func:`find_teams` palauttaa aina
:class:`TeamLookup`in, jossa osumia voi olla nolla, yksi tai monta. Hiljainen
"otetaan ensimmäinen" olisi juuri se virhe, jota vastaan sääntö on kirjoitettu:
divisioonan alkukirjain ``T`` osuu kolmeen joukkueeseen (``TUUHEE``,
``Takakeno``, ``Tankkiluola vilttiketju``), eikä mikään niistä ole
"todennäköisesti se oikea".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import datetime

__all__ = [
    "STEAM_ID64_LENGTH",
    "STEAM_ID64_BASE",
    "STEAM_ID64_MAX",
    "is_steam_id64",
    "RosterMember",
    "TeamObservation",
    "Team",
    "TeamLookup",
    "build_teams",
    "find_teams",
    "assign_lineup_keys",
]

#: SteamID64:n pituus merkkeinä. Kaikki CS2-tilit ovat tässä pituudessa.
STEAM_ID64_LENGTH = 17

#: Pienin mahdollinen SteamID64 (``STEAM_0:0:0``, universumi 1, tyyppi 1).
#:
#: Tarkistus on väli eikä pelkkä numeroisuus, koska ``game_player_id`` on
#: **lähteen antama merkkijono** eikä tämän ohjelman kirjoittama arvo. Pelkkä
#: "17 numeroa" hyväksyisi minkä tahansa 17-numeroisen luvun, ja väärä tunniste
#: näyttäisi myöhemmin tyhjältä leikkaukselta demoihin -- ei virheeltä.
STEAM_ID64_BASE = 76561197960265728

#: Suurin mahdollinen yksilötilin SteamID64: :data:`STEAM_ID64_BASE` plus
#: 32-bittisen tilitunnuksen suurin arvo (``0xFFFFFFFF``).
#:
#: **Yläraja on yhtä tarpeellinen kuin alaraja.** Ilman sitä esimerkiksi
#: ``"99999999999999999"`` kelpaisi tunnisteeksi: se on 17 numeroa ja suurempi
#: kuin alaraja, mutta se ei ole yhdenkään olemassa olevan tilin tunniste.
STEAM_ID64_MAX = STEAM_ID64_BASE + 0xFFFFFFFF


def is_steam_id64(value: object) -> bool:
    """Onko arvo SteamID64-muotoinen yksilötilin tunniste?

    >>> is_steam_id64("76561197977479426")
    True
    >>> is_steam_id64("f56dd02a-6107-48e2-abfb-75e7ec7ebcb2")
    False
    >>> is_steam_id64("12345678901234567")
    False
    >>> is_steam_id64("99999999999999999")
    False
    """
    if not isinstance(value, str) or len(value) != STEAM_ID64_LENGTH:
        return False
    if not value.isdigit():
        return False
    return STEAM_ID64_BASE <= int(value) <= STEAM_ID64_MAX


@dataclass(frozen=True)
class RosterMember:
    """Yksi pelaaja joukkueen rosterissa.

    Attributes:
        game_player_id: **SteamID64 ja ainoa avain.** Sillä pelaaja liitetään
            demon kokoonpanotauluun.
        nickname: Useimmin havaittu nimimerkki, tai ``None``. Ihmiselle
            näytettävä nimi; ei koskaan avain, koska se voi vaihtua.
        player_id: Lähteen oma pelaajatunniste (FACEITillä UUID), tai ``None``.
            Mukana jäljitettävyyttä varten -- sillä pelaajan tiedot haetaan
            rajapinnasta, mutta demoihin se ei liity.
        alternative_nicknames: Muut havaitut nimimerkit. Nimimerkin vaihtuminen
            on havainto samalla tavalla kuin joukkueen nimen vaihtuminen, eikä
            sitä siksi piiloteta kummassakaan tapauksessa.
    """

    game_player_id: str
    nickname: str | None = None
    player_id: str | None = None
    alternative_nicknames: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_steam_id64(self.game_player_id):
            raise ValueError(
                f"Rosterin tunniste {self.game_player_id!r} ei ole "
                "SteamID64-muotoinen, joten sitä ei voi liittää demoihin."
            )

    @property
    def display_name(self) -> str:
        """Nimimerkki, tai tunniste jos nimimerkkiä ei havaittu."""
        return self.nickname if self.nickname else self.game_player_id


@dataclass(frozen=True)
class TeamObservation:
    """Yksi havainto joukkueesta yhdessä ottelussa.

    Tämä on moduulin **syöte** ja samalla se raja, jonka taakse lähteen sanasto
    jää. ``stages.discover`` muuntaa ottelut näiksi; testi rakentaa ne käsin.

    Attributes:
        faction_id: Joukkueen tunniste lähteessä. **Avain, ei identiteetti**:
            :func:`build_teams` päättää, mitkä tunnisteet ovat sama joukkue.
        match_id: Ottelu, josta havainto on. Sama joukkue esiintyy monessa.
        observed_at: Havainnon hetki (ottelun aikataulu tai alku), tai ``None``
            jos aikaa ei tiedetä. **Tämä on se, mikä tekee sanoista
            "ensimmäinen" ja "viimeisin" tosia.** Ilman sitä järjestys olisi
            ``match_id``-merkkijonojärjestys, ja FACEITin tunnisteet ovat
            UUID-pohjaisia -- eli järjestys olisi satunnainen ja "viimeksi
            havaittu joukkue" tarkoittaisi "aakkosissa viimeinen".
        name: Joukkueen nimi havaintona, tai ``None``.
        played: Onko ottelu pelattu. Vaikuttaa vain lukuun "pelattuja
            otteluita" -- **ei rosteriin**: pelattujen otteluiden määrä ei saa
            vaikuttaa siihen, tunnetaanko joukkue. Mitattu 2026-09-04,
            ``PotkukelkkaPeek``illä on yksi pelattu ottelu yhdestätoista ja
            silti täysi kahdeksan pelaajan vakirosteri.
        roster: Aloittajat lähteen järjestyksessä.
        substitutes: Vaihtopelaajat lähteen järjestyksessä.
    """

    faction_id: str
    match_id: str
    observed_at: datetime | None = None
    name: str | None = None
    played: bool = False
    roster: tuple[RosterMember, ...] = ()
    substitutes: tuple[RosterMember, ...] = ()

    @property
    def everyone(self) -> tuple[RosterMember, ...]:
        """Aloittajat ja vaihtopelaajat yhtenä listana."""
        return self.roster + self.substitutes


@dataclass(frozen=True)
class Team:
    """Yksi joukkue vakirostereineen.

    Attributes:
        team_key: **Kanoninen tunniste**: varhaisimman havainnon ``faction_id``.
            Ei muutu, kun joukkue saa uuden tunnisteen uudella kaudella.
        faction_ids: Kaikki lähteen tunnisteet, jotka tunnistettiin tähän
            joukkueeseen, varhaisin ensin. Yleensä yksi.
        name: Yleisimmin havaittu nimi, tai ``None`` jos nimeä ei havaittu.
        roster: Vakirosteri: aloittajat ja vaihtopelaajat yhdisteenä kaikista
            otteluista, **paitsi** ne, jotka on sittemmin havaittu toisessa
            joukkueessa. Järjestys on nimimerkin mukainen, nimettömät lopussa.
        released: Pelaajat, jotka havaittiin tässä joukkueessa mutta myöhemmin
            toisessa. Eivät ole rosterissa, mutta eivät myöskään kadonneet.
        shared_players: SteamID64:t, jotka toinen joukkue havaitsi **yhtä
            myöhään**. Nämä ovat yhä rosterissa, koska kiistaa ei ratkaista
            arpomalla -- mutta se, että kiista on olemassa, on luettavissa.
        match_ids: Kaikki ottelut, joissa joukkue esiintyi, aikajärjestyksessä.
        played_match_ids: Niistä pelatut.
        lineup_keys: Arkistosta tunnistetut kokoonpanotiivisteet, jos niitä on
            liitetty (:func:`assign_lineup_keys`). **Ei identiteetti vaan
            silta**: tiiviste vaihtuu yhdestäkin vaihdosta, ``team_key`` ei.
        alternative_names: Muut havaitut nimet ``name``n lisäksi. Nimenvaihto
            on havainto, jota ei piiloteta.
    """

    team_key: str
    faction_ids: tuple[str, ...] = ()
    name: str | None = None
    roster: tuple[RosterMember, ...] = ()
    released: tuple[RosterMember, ...] = ()
    shared_players: tuple[str, ...] = ()
    match_ids: tuple[str, ...] = ()
    played_match_ids: tuple[str, ...] = ()
    lineup_keys: tuple[str, ...] = ()
    alternative_names: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        """Nimi, tai tunniste jos nimeä ei havaittu."""
        return self.name if self.name else self.team_key

    @property
    def player_ids(self) -> frozenset[str]:
        """Vakirosteri SteamID64-joukkona -- se, mikä liittyy demoihin."""
        return frozenset(member.game_player_id for member in self.roster)

    @property
    def matches_played(self) -> int:
        return len(self.played_match_ids)


@dataclass(frozen=True)
class TeamLookup:
    """Nimihaun tulos. **Monitulkintaisuus on tässä, ei poikkeuksessa.**

    Attributes:
        query: Haku sellaisenaan kuin käyttäjä sen kirjoitti.
        teams: Osumat. Nolla, yksi tai monta -- kaikki kolme ovat kelvollisia
            tuloksia, ja kutsuja päättää mitä niistä seuraa.
        matched_by: Miten osumat löytyivät (``"name"``, ``"team_key"``,
            ``"prefix"`` tai ``"contains"``), tai ``None`` jos osumia ei ole.
            Mukana siksi, että "miksi juuri nämä" on luettavissa eikä
            arvattavissa.
    """

    query: str
    teams: tuple[Team, ...] = ()
    matched_by: str | None = None

    @property
    def is_unique(self) -> bool:
        return len(self.teams) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.teams) > 1

    @property
    def is_empty(self) -> bool:
        return not self.teams

    @property
    def team(self) -> Team:
        """Ainoa osuma.

        Raises:
            ValueError: Jos osumia ei ole tai niitä on monta. Kutsujan on
                kysyttävä valinta ennen tätä; hiljainen valinta on kielletty.
        """
        if not self.is_unique:
            raise ValueError(
                f"Haku {self.query!r} ei tuottanut yhtä ainoaa joukkuetta "
                f"vaan {len(self.teams)}. Valinta on kysyttävä."
            )
        return self.teams[0]


# -- Joukkueiden kokoaminen --------------------------------------------------


@dataclass
class _Faction:
    """Yhden lähdetunnisteen kertymä ennen kuin identiteetti on ratkaistu."""

    faction_id: str
    members: dict[str, RosterMember]
    nicknames: dict[str, dict[str, int]]
    names: dict[str, int]
    match_ids: list[str]
    played_match_ids: list[str]
    #: Pelaajan viimeisin havaintohetki tämän tunnisteen alla. ``None``
    #: tarkoittaa "aika ei tiedossa", ja se on eri asia kuin varhainen hetki.
    last_seen: dict[str, datetime | None]


def build_teams(
    observations: Iterable[TeamObservation], *, min_common: int
) -> tuple[Team, ...]:
    """Kokoa havainnoista joukkueet vakirostereineen.

    Kolme vaihetta, ja jokainen toteuttaa yhden moduulin säännöistä:

    1. **Kertymä tunnisteittain.** Rosteri on aloittajien ja vaihtopelaajien
       yhdiste, avaimena ``game_player_id``.
    2. **Identiteetti rosterista.** Kaksi tunnistetta ovat sama joukkue, kun
       niiden rosterit jakavat vähintään ``min_common`` pelaajaa. Vertailu
       tehdään **ankkuriin**, ei ketjuna: ketjuttaminen liittäisi kaksi eri
       joukkuetta toisiinsa yhden välissä olevan kokoonpanon kautta. Sama
       peruste kuin ``domain.aggregate.lineups_of_same_team``illa.
    3. **Siirtyneet pelaajat pois rosterista.** Pelaaja kuuluu siihen
       joukkueeseen, joka havaitsi hänet viimeksi; muissa hän on
       :attr:`Team.released`issä. Yhtä myöhäinen havainto kahdessa joukkueessa
       ei siirrä ketään -- se kirjataan :attr:`Team.shared_players`iin.

    Nimeksi tulee **useimmin havaittu** nimi; sama sääntö koskee nimimerkkejä.
    Tasatilanteessa aakkosjärjestys ratkaisee, jottei tulos riipu siitä, missä
    järjestyksessä ottelut sattuivat tulemaan.

    Args:
        observations: Havainnot missä tahansa järjestyksessä. Funktio järjestää
            ne itse ``observed_at``in mukaan, joten tulos ei riipu syötteen
            järjestyksestä.
        min_common: Vähimmäismäärä yhteisiä pelaajia, jolla kaksi
            lähdetunnistetta ovat sama joukkue
            (``[thresholds].team_identity_min_common``).
            **Avainsanaparametri**: paljas kokonaisluku havaintolistan perässä
            olisi vaihdettavissa mihin tahansa muuhun lukuun ilman että mikään
            huomauttaisi.

    Returns:
        Joukkueet nimen mukaan järjestettynä (nimettömät lopussa).

    Raises:
        ValueError: Jos ``min_common`` ei ole positiivinen. Nolla liittäisi
            jokaisen tunnisteen jokaiseen, eli koko divisioona olisi yksi
            joukkue.
    """
    if min_common < 1:
        raise ValueError(
            f"Joukkueiden liittämisen kynnyksen on oltava vähintään 1, oli "
            f"{min_common}. Nolla tekisi koko divisioonasta yhden joukkueen."
        )

    ordered = sorted(observations, key=_observation_order)
    factions = _collect(ordered)
    clusters = _cluster(factions, min_common)
    return tuple(sorted(_teams(clusters), key=_team_order))


def _observation_order(observation: TeamObservation) -> tuple[int, float, str, str]:
    """Aikajärjestys; ajattomat havainnot viimeisenä, sitten ``match_id``.

    Ajaton havainto ei ole "vanhin" vaan "ei tiedossa", joten se ei saa
    ratkaista, mikä tunniste on kanoninen.
    """
    moment = observation.observed_at
    if moment is None:
        return (1, 0.0, observation.match_id, observation.faction_id)
    return (0, moment.timestamp(), observation.match_id, observation.faction_id)


def _collect(ordered: Sequence[TeamObservation]) -> list[_Faction]:
    """Kerää havainnot lähdetunnisteittain, aikajärjestys säilyttäen."""
    factions: dict[str, _Faction] = {}
    for observation in ordered:
        faction = factions.get(observation.faction_id)
        if faction is None:
            faction = _Faction(
                faction_id=observation.faction_id,
                members={},
                nicknames={},
                names={},
                match_ids=[],
                played_match_ids=[],
                last_seen={},
            )
            factions[observation.faction_id] = faction

        for member in observation.everyone:
            faction.members.setdefault(member.game_player_id, member)
            if member.nickname:
                counts = faction.nicknames.setdefault(member.game_player_id, {})
                counts[member.nickname] = counts.get(member.nickname, 0) + 1
            faction.last_seen[member.game_player_id] = _later(
                faction.last_seen.get(member.game_player_id),
                observation.observed_at,
                seen=member.game_player_id in faction.last_seen,
            )

        if observation.name:
            faction.names[observation.name] = faction.names.get(observation.name, 0) + 1
        if observation.match_id not in faction.match_ids:
            faction.match_ids.append(observation.match_id)
        if observation.played and observation.match_id not in faction.played_match_ids:
            faction.played_match_ids.append(observation.match_id)
    return list(factions.values())


def _later(
    known: datetime | None, candidate: datetime | None, *, seen: bool
) -> datetime | None:
    """Myöhempi kahdesta hetkestä; **tuntematon voittaa tunnetun**.

    Tuntematon hetki ei ole varhainen hetki: jos pelaaja on havaittu kerran
    ilman aikaa, emme voi väittää tietävämme, milloin hänet nähtiin viimeksi.
    Sen pitäminen ``None``ina jättää hänet kiistanalaiseksi eikä siirrä häntä
    väärään joukkueeseen.
    """
    if not seen:
        return candidate
    if known is None or candidate is None:
        return None
    return max(known, candidate)


def _cluster(factions: Sequence[_Faction], min_common: int) -> list[list[_Faction]]:
    """Ryhmittele lähdetunnisteet joukkueiksi rosterin perusteella.

    Tunniste liitetään siihen ryhmään, jonka **ankkurin** rosterin kanssa sillä
    on eniten yhteisiä pelaajia, kun yhteisiä on vähintään ``min_common``.
    Ankkuri on ryhmän varhaisin tunniste, ja vertailu tehdään aina siihen --
    ei ryhmän kasvaneeseen yhdisteeseen. Näin liittäminen ei ketjuunnu: A--B ja
    B--C eivät tee A:sta ja C:stä samaa joukkuetta, ellei A jaa kynnyksen
    verran pelaajia myös C:n kanssa.
    """
    clusters: list[list[_Faction]] = []
    anchors: list[frozenset[str]] = []
    for faction in factions:
        players = frozenset(faction.members)
        best_index = -1
        best_common = min_common - 1
        for index, anchor in enumerate(anchors):
            common = len(players & anchor)
            if common > best_common:
                best_common = common
                best_index = index
        if best_index < 0:
            clusters.append([faction])
            anchors.append(players)
        else:
            clusters[best_index].append(faction)
    return clusters


def _teams(clusters: Sequence[Sequence[_Faction]]) -> list[Team]:
    """Muunna ryhmät joukkueiksi ja ratkaise siirtyneet pelaajat."""
    #: Pelaajan viimeisin havaintohetki **jokaisessa** joukkueessa. Tarvitaan
    #: ennen kuin yhtäkään rosteria voi rajata: siirtyminen on kahden
    #: joukkueen välinen asia, eikä se näy kummastakaan yksin.
    latest: dict[str, dict[int, datetime | None]] = {}
    for index, cluster in enumerate(clusters):
        for faction in cluster:
            for player, moment in faction.last_seen.items():
                per_team = latest.setdefault(player, {})
                per_team[index] = _later(
                    per_team.get(index), moment, seen=index in per_team
                )

    teams: list[Team] = []
    for index, cluster in enumerate(clusters):
        merged = _merge_factions(cluster)
        roster: list[RosterMember] = []
        released: list[RosterMember] = []
        shared: list[str] = []
        for player, member in merged.members.items():
            with_names = _merge_member(member, merged.nicknames.get(player, {}))
            verdict = _belongs(latest.get(player, {}), index)
            if verdict == "released":
                released.append(with_names)
                continue
            if verdict == "shared":
                shared.append(player)
            roster.append(with_names)

        teams.append(
            Team(
                team_key=cluster[0].faction_id,
                faction_ids=tuple(faction.faction_id for faction in cluster),
                name=_best_key(merged.names),
                roster=tuple(sorted(roster, key=_member_order)),
                released=tuple(sorted(released, key=_member_order)),
                shared_players=tuple(sorted(shared)),
                match_ids=tuple(merged.match_ids),
                played_match_ids=tuple(merged.played_match_ids),
                alternative_names=_other_keys(merged.names),
            )
        )
    return teams


def _merge_factions(cluster: Sequence[_Faction]) -> _Faction:
    """Yhdistä ryhmän tunnisteet yhdeksi kertymäksi, järjestys säilyttäen."""
    merged = _Faction(
        faction_id=cluster[0].faction_id,
        members={},
        nicknames={},
        names={},
        match_ids=[],
        played_match_ids=[],
        last_seen={},
    )
    for faction in cluster:
        for player, member in faction.members.items():
            merged.members.setdefault(player, member)
        for player, counts in faction.nicknames.items():
            target = merged.nicknames.setdefault(player, {})
            for nickname, count in counts.items():
                target[nickname] = target.get(nickname, 0) + count
        for name, count in faction.names.items():
            merged.names[name] = merged.names.get(name, 0) + count
        for match_id in faction.match_ids:
            if match_id not in merged.match_ids:
                merged.match_ids.append(match_id)
        for match_id in faction.played_match_ids:
            if match_id not in merged.played_match_ids:
                merged.played_match_ids.append(match_id)
    return merged


def _belongs(per_team: Mapping[int, datetime | None], index: int) -> str:
    """Kuuluuko pelaaja tähän joukkueeseen: ``own``, ``shared`` vai ``released``?

    * Yhdessä joukkueessa havaittu pelaaja on aina ``own`` -- pelaaja, joka on
      ollut mukana vain kolmessa ottelussa yhdestätoista, ei ole siirtynyt
      minnekään.
    * Useammassa havaittu kuuluu sille, joka näki hänet **myöhimmin**.
    * Yhtä myöhäinen -- tai tuntematon -- havainto on ``shared``: kiista, jota
      ei ratkaista arpomalla.
    """
    if len(per_team) <= 1:
        return "own"
    mine = per_team.get(index)
    others = [moment for team, moment in per_team.items() if team != index]
    if mine is None or any(moment is None for moment in others):
        return "shared"
    newest = max(moment for moment in others if moment is not None)
    if mine > newest:
        return "own"
    if mine == newest:
        return "shared"
    return "released"


def _merge_member(member: RosterMember, counts: Mapping[str, int]) -> RosterMember:
    """Pelaaja, jonka nimimerkki on useimmin havaittu ja muut ovat tallessa."""
    if not counts:
        return member
    return replace(
        member,
        nickname=_best_key(counts),
        alternative_nicknames=_other_keys(counts),
    )


def _best_key(counts: Mapping[str, int]) -> str | None:
    """Useimmin havaittu arvo; tasatilanteessa aakkosjärjestyksen ensimmäinen.

    Aakkosjärjestys on ``casefold``attu samoin kuin joukkueiden järjestys
    (:func:`_team_order`), jottei samassa moduulissa olisi kahta eri
    aakkosjärjestystä.
    """
    if not counts:
        return None
    return min(counts, key=lambda value: (-counts[value], value.casefold(), value))


def _other_keys(counts: Mapping[str, int]) -> tuple[str, ...]:
    best = _best_key(counts)
    return tuple(sorted((value for value in counts if value != best), key=str.casefold))


def _member_order(member: RosterMember) -> tuple[int, str, str]:
    """Nimimerkin mukaan, nimettömät lopussa.

    Vertailu on nimimerkki **sellaisenaan** eikä pienaakkosiksi muutettuna:
    juuri se tuottaa mittausdokumentin (luku 3) luettelemat rosterit siinä
    järjestyksessä kuin ne siellä lukevat. Tämä on eri asia kuin
    :func:`_best_key`in tasatilanteen ratkaisu, joka ei ole näytettävä
    järjestys vaan valinta kahden yhtä usein havaitun arvon väliltä.
    """
    if member.nickname is None:
        return (1, "", member.game_player_id)
    return (0, member.nickname, member.game_player_id)


def _team_order(team: Team) -> tuple[int, str, str]:
    if team.name is None:
        return (1, "", team.team_key)
    return (0, team.name.casefold(), team.team_key)


# -- Nimihaku ----------------------------------------------------------------


def find_teams(teams: Sequence[Team], query: str) -> TeamLookup:
    """Etsi joukkue nimellä. **Kirjainkoosta riippumatta, hiljaa valitsematta.**

    Haku etenee tarkimmasta väljimpään, ja **ensimmäinen osuva taso ratkaisee**:

    1. nimi täsmälleen (kirjainkoosta riippumatta),
    2. tunniste täsmälleen -- sekä kanoninen ``team_key`` että mikä tahansa
       :attr:`Team.faction_ids`in tunniste, jotta vanhalla kausitunnisteella
       löytää yhä saman joukkueen,
    3. nimen alku,
    4. nimen sisältä.

    Portaikko on siinä siksi, ettei täsmällinen nimi jäisi monitulkintaiseksi
    vain siksi, että se sattuu olemaan toisen nimen alku. Jos taso tuottaa
    monta osumaa, tulos on monitulkintainen -- myös silloin kun kaksi
    joukkuetta on samannimistä. Valintaa **ei tehdä täällä**.

    Args:
        teams: Joukkueet, tavallisesti :func:`build_teams`in tulos.
        query: Käyttäjän kirjoittama nimi, sen osa tai tunniste.

    Returns:
        :class:`TeamLookup`, jonka osumat ovat samassa järjestyksessä kuin
        ``teams``.
    """
    needle = query.strip().casefold()
    if not needle:
        return TeamLookup(query=query)

    tiers: tuple[tuple[str, list[Team]], ...] = (
        ("name", [t for t in teams if t.name and t.name.casefold() == needle]),
        (
            "team_key",
            [
                t
                for t in teams
                if t.team_key.casefold() == needle
                or any(key.casefold() == needle for key in t.faction_ids)
            ],
        ),
        (
            "prefix",
            [t for t in teams if t.name and t.name.casefold().startswith(needle)],
        ),
        ("contains", [t for t in teams if t.name and needle in t.name.casefold()]),
    )
    for matched_by, found in tiers:
        if found:
            return TeamLookup(query=query, teams=tuple(found), matched_by=matched_by)
    return TeamLookup(query=query)


# -- Silta arkistoon ---------------------------------------------------------


def assign_lineup_keys(
    teams: Sequence[Team],
    lineups: Mapping[str, Set[str]],
    min_common: int,
) -> tuple[tuple[Team, ...], tuple[str, ...]]:
    """Liitä arkiston kokoonpanotiivisteet joukkueisiin rosterin perusteella.

    Silta on rakennettava, koska arkiston hakemistot on nimetty
    kokoonpanotiivisteestä ja tämä tarina **ei nimeä niitä uudelleen** (se
    päätös on Story 3.4). Ilman siltaa ``index/teams.json`` ja
    ``aggregates/<team_key>`` olisivat kaksi toisistaan tietämätöntä maailmaa.

    Sääntö on **sama kuin** ``domain.aggregate.lineups_of_same_team``illa:
    tiiviste liitetään **jokaiseen** joukkueeseen, jonka vakirosterin kanssa
    sillä on vähintään ``min_common`` yhteistä pelaajaa. Aiemmin tässä oli
    "eniten yhteisiä voittaa", ja se tarkoitti, että sama asetusarvo
    (``team_identity_min_common``) merkitsi kahdessa paikassa kahta eri asiaa.

    Kynnyksen ylittäminen kahdessa joukkueessa on **aito monitulkintaisuus**
    eikä sitä ratkaista arpomalla. Tiivisteet, jotka useampi joukkue omistaa,
    palautetaan erikseen, jottei jatkovaihe laskisi niitä kahdesti tietämättä
    tekevänsä niin.

    Args:
        teams: Joukkueet, joihin liitetään.
        lineups: ``lineup_key`` -> pelaajien SteamID64-joukko, luettuna
            arkiston kokoonpanotauluista.
        min_common: Vähimmäismäärä yhteisiä pelaajia
            (``[thresholds].team_identity_min_common``).

    Returns:
        ``(joukkueet, kiistanalaiset)``. Joukkueet ovat samassa järjestyksessä
        kuin sisään tullessaan, ``lineup_keys`` täytettynä. Kiistanalaiset ovat
        ne tiivisteet, jotka useampi kuin yksi joukkue omistaa.
    """
    assigned: dict[str, list[str]] = {}
    contested: list[str] = []
    for lineup_key, players in sorted(lineups.items()):
        owners = [
            team.team_key
            for team in teams
            if len(team.player_ids & set(players)) >= min_common
        ]
        for team_key in owners:
            assigned.setdefault(team_key, []).append(lineup_key)
        if len(owners) > 1:
            contested.append(lineup_key)

    updated = tuple(
        replace(team, lineup_keys=tuple(sorted(assigned.get(team.team_key, ()))))
        for team in teams
    )
    return updated, tuple(contested)
