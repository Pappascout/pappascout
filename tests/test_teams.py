"""``domain.teams`` -- nimihaun ja vakirosterin testit (Story 3.2).

**Oikeat nimet, ei keksittyjä.** Divisioonan 12 joukkuetta ja niiden mitatut
rosterikoot ovat ``mittaus-faceit-aineisto.md``in luvuista 3 ja 4, ja ne on
kirjoitettu tänne sellaisinaan. Keksityllä joukkueluettelolla nimihaun
monitulkintaisuus olisi teoreettinen tapaus, jonka voisi vahingossa tehdä
helpoksi; oikeilla nimillä se on se, mitä käyttäjä oikeasti kirjoittaa.

Rcave Veteransin neljä SteamID64:ää ovat mitattuja (luku 2, leikkaus arkiston
``lineups.parquet``in kanssa). Loput on **rakennettu** juoksevasta luvusta --
ne ovat oikean muotoisia mutteivät oikeita tilejä, ja se sanotaan tässä ääneen,
jottei niitä myöhemmin luulisi mittaukseksi.

Yksikään testi ei käy verkossa eikä levyllä: moduuli on puhdas, ja havainnot
rakennetaan käsin.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pappascout.domain.teams import (
    STEAM_ID64_BASE,
    STEAM_ID64_MAX,
    RosterMember,
    Team,
    TeamObservation,
    assign_lineup_keys,
    build_teams,
    find_teams,
    is_steam_id64,
)

#: Divisioonan joukkueet ja niiden **mitatut** vakirosterikoot (luku 3).
DIVISION: dict[str, int] = {
    "popsiCS": 9,
    "JYSAEYTTAEJAET": 8,
    "PotkukelkkaPeek": 8,
    "Suhisevat Sukat": 8,
    "TUUHEE": 8,
    "Takakeno": 8,
    "YllatysMomentti": 8,
    "cM Esports": 8,
    "KASIKAASU": 7,
    "Rcave Veterans": 7,
    "uncs67": 7,
    "Tankkiluola vilttiketju": 6,
}

#: Mitatut nimimerkit kahdelta joukkueelta (luku 3).
RCAVE = (
    "HCNoRage",
    "Kronnennn",
    "Lindberq_",
    "MarkusN",
    "SSStttNNN",
    "bobb_y",
    "pornopertti",
)
POTKU = (
    "-Kurittaja-",
    "Jekkuekku",
    "Kisuisukki",
    "MyrkkyPena",
    "Patteri",
    "miicco",
    "progepanda",
    "wormi27z",
)

#: Mitatut SteamID64:t: nämä neljä löytyivät sekä FACEITista että arkiston
#: demosta ``ANCIENT_vs_RCAVE_VETERANS`` (luku 2). Koordinaattorin live-ajossa
#: leikkaus oli lopulta **5**, kun varapelaajat luettiin mukaan.
MEASURED_RCAVE_IDS = {
    "SSStttNNN": "76561197977479426",
    "pornopertti": "76561197985923425",
    "HCNoRage": "76561197993527314",
    "bobb_y": "76561198062941501",
}

#: Round robin, 12 joukkuetta: 11 ottelua per joukkue.
MATCHES_PER_TEAM = 11

#: Kynnys, jolla kaksi lähdetunnistetta ovat sama joukkue
#: (``[thresholds].team_identity_min_common``, oletus 3).
MIN_COMMON = 3

KICKOFF = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)


def steam_id(number: int) -> str:
    """Rakennettu mutta oikean muotoinen SteamID64."""
    return str(STEAM_ID64_BASE + number)


def nicknames(team_name: str, size: int) -> tuple[str, ...]:
    if team_name == "Rcave Veterans":
        return RCAVE
    if team_name == "PotkukelkkaPeek":
        return POTKU
    slug = team_name.split()[0].lower()
    return tuple(f"{slug}{index}" for index in range(1, size + 1))


def members(team_name: str, size: int, offset: int) -> tuple[RosterMember, ...]:
    return tuple(
        RosterMember(
            game_player_id=MEASURED_RCAVE_IDS.get(nick, steam_id(offset + index)),
            nickname=nick,
            player_id=f"uuid-{offset + index}",
        )
        for index, nick in enumerate(nicknames(team_name, size))
    )


def division_observations(
    played: dict[str, int] | None = None,
) -> list[TeamObservation]:
    """Divisioonan havainnot: 12 joukkuetta, 11 ottelua kummallekin.

    Vakirosteri **hajautetaan otteluiden kesken**: jokaisessa ottelussa on viisi
    aloittajaa ja loput vaihtopelaajina, ja aloittajajoukko kiertää. Näin
    yhdenkään ottelun rivi ei yksin sisällä koko rosteria -- eli testi mittaa
    yhdistettä eikä viimeisintä ottelua.
    """
    played = played or {}
    observations: list[TeamObservation] = []
    for team_index, (team_name, size) in enumerate(DIVISION.items()):
        roster = members(team_name, size, offset=team_index * 100)
        played_count = played.get(team_name, MATCHES_PER_TEAM)
        for match_no in range(MATCHES_PER_TEAM):
            shift = match_no % size
            rotated = roster[shift:] + roster[:shift]
            observations.append(
                TeamObservation(
                    faction_id=f"faction-{team_index:02d}",
                    match_id=f"1-t{team_index:02d}-m{match_no:02d}",
                    observed_at=KICKOFF + timedelta(days=match_no),
                    name=team_name,
                    played=match_no < played_count,
                    roster=rotated[:5],
                    substitutes=rotated[5:],
                )
            )
    return observations


@pytest.fixture
def division() -> tuple[Team, ...]:
    """Divisioona, jossa PotkukelkkaPeek on pelannut 1 ottelun 11:stä."""
    return build_teams(
        division_observations(played={"PotkukelkkaPeek": 1}), min_common=MIN_COMMON
    )


# -- SteamID64 on rosterin ainoa tunniste -----------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("76561197977479426", True),
        ("76561198062941501", True),
        (str(STEAM_ID64_BASE), True),
        (str(STEAM_ID64_MAX), True),
        # FACEITin oma player_id on UUID eikä esiinny demoissa lainkaan.
        ("f56dd02a-6107-48e2-abfb-75e7ec7ebcb2", False),
        # Oikea pituus, mutta pienempi kuin pienin mahdollinen SteamID64.
        ("12345678901234567", False),
        # Oikea pituus ja suurempi kuin alaraja, mutta suurempi kuin yhdenkään
        # olemassa olevan tilin tunniste. Ilman ylärajaa tämä kelpaisi.
        ("99999999999999999", False),
        (str(STEAM_ID64_MAX + 1), False),
        ("7656119797747942", False),
        ("765611979774794267", False),
        ("", False),
        (76561197977479426, False),
        (None, False),
    ],
)
def test_only_steam_id64_shaped_values_are_identifiers(
    value: object, expected: bool
) -> None:
    assert is_steam_id64(value) is expected


def test_the_upper_bound_is_the_largest_possible_account_id() -> None:
    """Yläraja ei ole arvattu: se on tilitunnuksen 32-bittinen katto."""
    assert STEAM_ID64_MAX == STEAM_ID64_BASE + 0xFFFFFFFF
    assert len(str(STEAM_ID64_MAX)) == 17


def test_a_roster_member_without_a_steam_id_cannot_exist() -> None:
    """Tunniste on avain: väärän muotoinen ei liittyisi demoihin vaan katoaisi.

    Virhe tulee heti rakennettaessa eikä vasta liitoksessa, jossa se näyttäisi
    tyhjältä leikkaukselta -- eli havainnolta eikä virheeltä.
    """
    with pytest.raises(ValueError, match="SteamID64"):
        RosterMember(game_player_id="f56dd02a-6107-48e2-abfb-75e7ec7ebcb2")


def test_every_identifier_in_every_roster_is_steam_id64_shaped(
    division: tuple[Team, ...],
) -> None:
    """Hyväksymiskriteeri: jokainen tunniste on SteamID64-muotoinen."""
    for team in division:
        assert team.player_ids, team.team_key
        assert all(is_steam_id64(pid) for pid in team.player_ids), team.team_key


# -- Vakirosteri on yhdiste -------------------------------------------------


def test_the_standing_roster_is_the_union_not_the_latest_match() -> None:
    """I/O-matriisi: rosteri vaihtelee otteluiden välillä.

    Kolme ottelua, joissa jokaisessa viisi aloittajaa mutta eri viisi. Viimeisin
    ottelu antaisi viisi pelaajaa; yhdiste antaa seitsemän.
    """
    roster = members("Rcave Veterans", 7, offset=0)
    observations = [
        TeamObservation(
            faction_id="f56dd02a",
            match_id=f"1-m{index}",
            observed_at=KICKOFF + timedelta(days=index),
            name="Rcave Veterans",
            roster=roster[index : index + 5],
        )
        for index in range(3)
    ]

    (team,) = build_teams(observations, min_common=MIN_COMMON)

    assert len(team.roster) == 7
    assert team.player_ids == {member.game_player_id for member in roster}


def test_substitutes_are_part_of_the_standing_roster() -> None:
    """Mitattu: ilman ``substitutes``ia rosteri aliarvioi järjestelmällisesti.

    ``Lindberq_`` on arkiston demossa muttei kertaakaan ``roster``issa, joten
    pelkkä aloittajalista jättäisi hänet pois -- ja rosterikynnys (Story 3.3)
    laskisi siksi joka kerta yhden liian vähän. Koordinaattorin live-ajossa
    leikkaus oli varapelaajien kanssa 5/5 eikä 4/5.
    """
    roster = members("Rcave Veterans", 7, offset=0)
    starters = tuple(m for m in roster if m.nickname != "Lindberq_")[:5]
    lindberq = next(m for m in roster if m.nickname == "Lindberq_")

    (team,) = build_teams(
        [
            TeamObservation(
                faction_id="f56dd02a",
                match_id="1-m0",
                observed_at=KICKOFF,
                name="Rcave Veterans",
                roster=starters,
                substitutes=(lindberq,),
            )
        ],
        min_common=MIN_COMMON,
    )

    assert "Lindberq_" in [member.nickname for member in team.roster]


def test_a_missing_substitutes_list_is_not_an_error() -> None:
    """I/O-matriisi: ``substitutes`` puuttuu tai on tyhjä."""
    roster = members("KASIKAASU", 5, offset=0)

    (team,) = build_teams(
        [
            TeamObservation(
                faction_id="k",
                match_id="1-m0",
                observed_at=KICKOFF,
                name="KASIKAASU",
                roster=roster,
            )
        ],
        min_common=MIN_COMMON,
    )

    assert len(team.roster) == 5


def test_the_number_of_played_matches_does_not_change_the_roster(
    division: tuple[Team, ...],
) -> None:
    """Hyväksymiskriteeri: PotkukelkkaPeek, 1 pelattu ottelu 11:stä.

    Vakirosteri on täysi, koska se luetaan **kaikista** otteluista -- myös
    pelaamattomista. Jos rosteri riippuisi pelatuista otteluista, kauden alussa
    yksikään joukkue ei olisi tunnistettavissa.
    """
    lookup = find_teams(division, "PotkukelkkaPeek")

    assert lookup.is_unique
    assert lookup.team.matches_played == 1
    assert len(lookup.team.match_ids) == MATCHES_PER_TEAM
    assert [m.nickname for m in lookup.team.roster] == list(POTKU)


def test_every_roster_in_the_division_is_between_six_and_nine_players(
    division: tuple[Team, ...],
) -> None:
    """Hyväksymiskriteeri: rosterit 6--9 pelaajaa. Epicin lupaus oli 5--10."""
    sizes = {team.display_name: len(team.roster) for team in division}

    assert sizes == DIVISION
    assert min(sizes.values()) == 6
    assert max(sizes.values()) == 9


def test_building_teams_does_not_depend_on_observation_order() -> None:
    """Sama syöte eri järjestyksessä on sama tulos -- muuten indeksi heiluisi."""
    observations = division_observations()

    assert build_teams(observations, min_common=MIN_COMMON) == build_teams(
        list(reversed(observations)), min_common=MIN_COMMON
    )


# -- Identiteetti on rosteri, ei tunniste -----------------------------------


def two_seasons(
    *, shared: int, first_key: str = "kausi-12", second_key: str = "kausi-13"
) -> list[TeamObservation]:
    """Sama joukkue kahdella kaudella, kaksi eri lähdetunnistetta.

    ``shared`` kertoo, montako viiden pelaajan rosterista on sama molemmilla
    kausilla. Loput ovat uusia pelaajia.
    """
    old = members("kausi", 5, offset=0)
    new = old[:shared] + members("uusi", 5, offset=500)[shared:]
    return [
        TeamObservation(
            faction_id=first_key,
            match_id="1-vanha",
            observed_at=KICKOFF,
            name="Rcave Veterans",
            roster=old,
        ),
        TeamObservation(
            faction_id=second_key,
            match_id="1-uusi",
            observed_at=KICKOFF + timedelta(days=365),
            name="Rcave Veterans",
            roster=new,
        ),
    ]


def test_a_new_season_identifier_is_the_same_team_when_the_roster_stays() -> None:
    """Epicin AC3: nimen tai divisioonan vaihto ei katkaise identiteettiä.

    Uusi kausi antaa samalle porukalle uuden ``faction_id``:n. Jos tunniste
    olisi identiteetti, tuloksena olisi kaksi joukkuetta, joita mikään ei
    yhdistä -- ja koko arkiston historia katkeaisi kauden vaihtuessa.

    **Tätä ei voi todentaa live-aineistoa vasten**: asetuksissa on yksi
    championship, ja mitattu tulos oli tasan yksi tunniste per joukkue.
    """
    teams = build_teams(two_seasons(shared=4), min_common=MIN_COMMON)

    assert len(teams) == 1
    assert teams[0].faction_ids == ("kausi-12", "kausi-13")


def test_the_canonical_key_is_the_earliest_identifier_and_does_not_change() -> None:
    """``team_key`` ei muutu, kun uusi kausi tuo uuden tunnisteen.

    Kanoninen tunniste on **varhaisimman havainnon** tunniste, ja järjestys
    tulee ``observed_at``ista -- ei ``match_id``-merkkijonosta, joka FACEITin
    UUID-pohjaisilla tunnisteilla olisi satunnainen.
    """
    teams = build_teams(two_seasons(shared=4), min_common=MIN_COMMON)

    assert teams[0].team_key == "kausi-12"


def test_two_identifiers_below_the_threshold_stay_two_teams() -> None:
    """Kaksi yhteistä pelaajaa ei ole sama joukkue vaan sattuma.

    Tämä on liittämisen vastinpari: ilman kynnystä mikä tahansa yhteinen
    pelaaja sulauttaisi kaksi eri joukkuetta yhdeksi.
    """
    teams = build_teams(two_seasons(shared=2), min_common=MIN_COMMON)

    assert len(teams) == 2


def test_joining_is_never_chained_through_a_middle_roster() -> None:
    """A--B ja B--C eivät tee A:sta ja C:stä samaa joukkuetta.

    Ketjuttaminen liittäisi kaksi eri joukkuetta toisiinsa yhden välissä olevan
    kokoonpanon kautta -- sama peruste kuin
    ``domain.aggregate.lineups_of_same_team``illa.
    """
    pool = members("pool", 9, offset=0)
    observations = [
        TeamObservation(
            faction_id="A",
            match_id="1-a",
            observed_at=KICKOFF,
            name="A",
            roster=pool[0:5],
        ),
        TeamObservation(
            faction_id="B",
            match_id="1-b",
            observed_at=KICKOFF + timedelta(days=1),
            name="B",
            roster=pool[2:7],
        ),
        TeamObservation(
            faction_id="C",
            match_id="1-c",
            observed_at=KICKOFF + timedelta(days=2),
            name="C",
            roster=pool[4:9],
        ),
    ]

    teams = build_teams(observations, min_common=MIN_COMMON)

    # B liittyy A:han (3 yhteistä). C jakaa A:n kanssa vain yhden, joten se jää
    # omakseen, vaikka se jakaisi B:n kanssa kolme.
    keys = {team.team_key: team.faction_ids for team in teams}
    assert keys == {"A": ("A", "B"), "C": ("C",)}


def test_a_zero_threshold_would_make_the_division_one_team_and_is_refused() -> None:
    with pytest.raises(ValueError, match="vähintään 1"):
        build_teams(division_observations(), min_common=0)


def test_an_old_season_identifier_still_finds_the_team() -> None:
    """Vanha tunniste on yhä avain, vaikkei se enää ole kanoninen."""
    teams = build_teams(two_seasons(shared=4), min_common=MIN_COMMON)

    lookup = find_teams(teams, "kausi-13")

    assert lookup.is_unique
    assert lookup.team.team_key == "kausi-12"


# -- Siirtyvä pelaaja -------------------------------------------------------


def transfer_observations(
    *, second_moment: datetime | None, first_moment: datetime | None = KICKOFF
) -> list[TeamObservation]:
    """Pelaaja ``siirtyja`` havaitaan ensin joukkueessa A ja sitten B:ssä."""
    a_players = members("aaa", 5, offset=0)
    b_players = members("bbb", 5, offset=100)
    mover = a_players[0]
    return [
        TeamObservation(
            faction_id="A",
            match_id="1-a",
            observed_at=first_moment,
            name="Aakkoset",
            roster=a_players,
        ),
        TeamObservation(
            faction_id="B",
            match_id="1-b",
            observed_at=second_moment,
            name="Beeta",
            roster=b_players[:4] + (mover,),
        ),
    ]


def test_a_player_who_moved_leaves_the_old_roster() -> None:
    """Katselmus: yhdiste kaikista otteluista jätti siirtyjän molempiin.

    Se paisuttaisi rostereita ja vääristäisi sekä rosterikynnystä (Story 3.3)
    että joukkueiden liittämistä -- kaksi eri joukkuetta alkaisi näyttää
    samalta, koska molemmilla olisi sama pelaaja.
    """
    teams = build_teams(
        transfer_observations(second_moment=KICKOFF + timedelta(days=30)),
        min_common=MIN_COMMON,
    )

    by_name = {team.display_name: team for team in teams}
    mover = members("aaa", 5, offset=0)[0].game_player_id

    assert mover not in by_name["Aakkoset"].player_ids
    assert mover in by_name["Beeta"].player_ids


def test_the_old_team_still_remembers_the_player_who_left() -> None:
    """Pudotus ei ole poisto: havainto säilyy ``released``issä."""
    teams = build_teams(
        transfer_observations(second_moment=KICKOFF + timedelta(days=30)),
        min_common=MIN_COMMON,
    )

    by_name = {team.display_name: team for team in teams}
    released = [m.nickname for m in by_name["Aakkoset"].released]

    assert released == ["aaa1"]
    assert by_name["Beeta"].released == ()


def test_an_equally_recent_observation_moves_nobody() -> None:
    """Kahden joukkueen yhtä myöhäinen havainto on kiista, ei siirtymä.

    Kiistaa ei ratkaista arpomalla: pelaaja jää molempiin rostereihin ja se,
    että kiista on olemassa, on luettavissa.
    """
    teams = build_teams(
        transfer_observations(second_moment=KICKOFF), min_common=MIN_COMMON
    )

    mover = members("aaa", 5, offset=0)[0].game_player_id
    for team in teams:
        assert mover in team.player_ids
        assert team.shared_players == (mover,)
        assert team.released == ()


def test_an_unknown_observation_time_moves_nobody() -> None:
    """Ilman aikaa ei voi väittää tietävänsä, kumpi havainto oli myöhempi."""
    teams = build_teams(
        transfer_observations(second_moment=None), min_common=MIN_COMMON
    )

    mover = members("aaa", 5, offset=0)[0].game_player_id
    assert all(mover in team.player_ids for team in teams)
    assert all(team.shared_players == (mover,) for team in teams)


def test_a_player_seen_in_only_one_team_is_never_released(
    division: tuple[Team, ...],
) -> None:
    """Kolme ottelua yhdestätoista ei ole siirtymä vaan vähän peliaikaa."""
    assert all(team.released == () for team in division)
    assert all(team.shared_players == () for team in division)


# -- Nimihaku ---------------------------------------------------------------


def test_an_exact_name_finds_exactly_one_team(division: tuple[Team, ...]) -> None:
    """I/O-matriisi: täsmällinen nimi ``Rcave Veterans``."""
    lookup = find_teams(division, "Rcave Veterans")

    assert lookup.is_unique
    assert lookup.team.name == "Rcave Veterans"
    assert lookup.matched_by == "name"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("rcave veterans", "Rcave Veterans"),
        ("RCAVE VETERANS", "Rcave Veterans"),
        ("POTKUKELKKAPEEK", "PotkukelkkaPeek"),
        ("potkukelkkapeek", "PotkukelkkaPeek"),
        ("UNCS67", "uncs67"),
        ("cm esports", "cM Esports"),
        ("POPSICS", "popsiCS"),
        ("  Rcave Veterans  ", "Rcave Veterans"),
    ],
)
def test_case_does_not_matter(
    division: tuple[Team, ...], query: str, expected: str
) -> None:
    """I/O-matriisi: kirjainkoko eroaa.

    Divisioonan nimissä kirjainkoko vaihtelee aidosti (``uncs67``,
    ``cM Esports``, ``popsiCS``, ``JYSAEYTTAEJAET``), joten tämä ei ole
    mukavuus vaan edellytys.
    """
    lookup = find_teams(division, query)

    assert lookup.is_unique
    assert lookup.team.name == expected


def test_an_unambiguous_partial_name_is_enough(division: tuple[Team, ...]) -> None:
    """I/O-matriisi: ``Rcave`` osuu yksikäsitteisesti yhteen."""
    lookup = find_teams(division, "Rcave")

    assert lookup.is_unique
    assert lookup.team.name == "Rcave Veterans"
    assert lookup.matched_by == "prefix"


def test_the_rcave_roster_is_the_seven_measured_players(
    division: tuple[Team, ...],
) -> None:
    """Hyväksymiskriteeri: haku ``Rcave`` -> yksi joukkue, rosteri 7 pelaajaa."""
    lookup = find_teams(division, "Rcave")

    assert [member.nickname for member in lookup.team.roster] == list(RCAVE)
    # Neljä mitattua tunnistetta ovat mukana sellaisinaan: juuri ne liittävät
    # rosterin arkiston demoon.
    assert set(MEASURED_RCAVE_IDS.values()) <= lookup.team.player_ids


def test_an_ambiguous_prefix_lists_all_three_and_chooses_none(
    division: tuple[Team, ...],
) -> None:
    """Hyväksymiskriteeri: ``T`` osuu kolmeen, eikä yhtään valita.

    Tämä on **mitattu tapaus eikä teoreettinen**: divisioonan 12 nimestä kolme
    alkaa T:llä.
    """
    lookup = find_teams(division, "T")

    assert lookup.is_ambiguous
    assert [team.name for team in lookup.teams] == [
        "Takakeno",
        "Tankkiluola vilttiketju",
        "TUUHEE",
    ]
    with pytest.raises(ValueError, match="Valinta on kysyttävä"):
        _ = lookup.team


def test_an_unknown_name_finds_nothing(division: tuple[Team, ...]) -> None:
    """I/O-matriisi: ``Astralis`` ei ole divisioonassa."""
    lookup = find_teams(division, "Astralis")

    assert lookup.is_empty
    assert lookup.matched_by is None


def test_two_teams_with_the_same_name_are_both_listed() -> None:
    """I/O-matriisi: sama nimi kahdella joukkueella.

    Teoreettinen mutta ei mahdoton. Täsmällinen nimi ei tee valintaa yhtään sen
    hiljaisemmin kuin osittainenkaan.
    """
    teams = build_teams(
        [
            TeamObservation(
                faction_id="faction-a",
                match_id="1-m0",
                observed_at=KICKOFF,
                name="Takakeno",
                roster=members("a", 5, offset=0),
            ),
            TeamObservation(
                faction_id="faction-b",
                match_id="1-m1",
                observed_at=KICKOFF,
                name="Takakeno",
                roster=members("b", 5, offset=50),
            ),
        ],
        min_common=MIN_COMMON,
    )

    lookup = find_teams(teams, "Takakeno")

    assert lookup.is_ambiguous
    assert {team.team_key for team in lookup.teams} == {"faction-a", "faction-b"}


def test_an_exact_name_wins_over_being_a_prefix_of_another() -> None:
    """Täsmällinen nimi ei jää monitulkintaiseksi toisen nimen alkuna.

    Ilman portaikkoa ``uncs67`` osuisi myös joukkueeseen ``uncs67 Academy``, ja
    joukkueen omalla nimellä hakeminen olisi mahdotonta.
    """
    teams = build_teams(
        [
            TeamObservation(
                faction_id="a",
                match_id="1-m0",
                observed_at=KICKOFF,
                name="uncs67",
                roster=members("a", 5, 0),
            ),
            TeamObservation(
                faction_id="b",
                match_id="1-m1",
                observed_at=KICKOFF,
                name="uncs67 Academy",
                roster=members("b", 5, 50),
            ),
        ],
        min_common=MIN_COMMON,
    )

    lookup = find_teams(teams, "uncs67")

    assert lookup.is_unique
    assert lookup.team.team_key == "a"


def test_a_team_can_be_found_by_its_key(division: tuple[Team, ...]) -> None:
    """Tunniste kelpaa yhä hakuun -- indeksi lisää nimen, ei poista tunnistetta."""
    lookup = find_teams(division, "faction-09")

    assert lookup.is_unique
    assert lookup.team.team_key == "faction-09"


def test_a_name_can_be_found_from_the_middle(division: tuple[Team, ...]) -> None:
    """``vilttiketju`` on nimen loppuosa, ja sekin riittää kun se on yksiselitteinen."""
    lookup = find_teams(division, "vilttiketju")

    assert lookup.is_unique
    assert lookup.team.name == "Tankkiluola vilttiketju"
    assert lookup.matched_by == "contains"


def test_an_empty_query_finds_nothing_rather_than_everything(
    division: tuple[Team, ...],
) -> None:
    """Tyhjä haku osuisi kaikkiin, eikä "kaikki" ole hakutulos."""
    assert find_teams(division, "   ").is_empty


# -- Nimet ja nimimerkit ovat havaintoja ------------------------------------


def test_the_most_often_observed_name_wins_and_the_others_are_kept() -> None:
    """Nimenvaihtoa ei piiloteta: vaihtoehdot ovat luettavissa."""
    roster = members("x", 5, offset=0)
    observations = [
        TeamObservation(
            faction_id="a",
            match_id="1-m0",
            observed_at=KICKOFF,
            name="Vanha nimi",
            roster=roster,
        ),
        TeamObservation(
            faction_id="a",
            match_id="1-m1",
            observed_at=KICKOFF + timedelta(days=1),
            name="Uusi nimi",
            roster=roster,
        ),
        TeamObservation(
            faction_id="a",
            match_id="1-m2",
            observed_at=KICKOFF + timedelta(days=2),
            name="Uusi nimi",
            roster=roster,
        ),
    ]

    (team,) = build_teams(observations, min_common=MIN_COMMON)

    assert team.name == "Uusi nimi"
    assert team.alternative_names == ("Vanha nimi",)


def test_a_changed_nickname_is_kept_the_same_way_a_changed_team_name_is() -> None:
    """Katselmus: joukkueella oli ``alternative_names``, pelaajalla ei mitään.

    Nimimerkin vaihtuminen on täsmälleen samanlainen havainto kuin joukkueen
    nimen vaihtuminen, eikä kumpaakaan piiloteta.
    """
    old = RosterMember(game_player_id=steam_id(1), nickname="vanha", player_id="u1")
    new = RosterMember(game_player_id=steam_id(1), nickname="uusi", player_id="u1")
    rest = members("x", 5, offset=100)[1:]
    observations = [
        TeamObservation(
            faction_id="a",
            match_id=f"1-m{index}",
            observed_at=KICKOFF + timedelta(days=index),
            name="Joukkue",
            roster=(member,) + rest,
        )
        for index, member in enumerate((old, new, new))
    ]

    (team,) = build_teams(observations, min_common=MIN_COMMON)
    player = next(m for m in team.roster if m.game_player_id == steam_id(1))

    assert player.nickname == "uusi"
    assert player.alternative_nicknames == ("vanha",)


def test_a_team_without_any_observed_name_falls_back_to_its_key() -> None:
    """Puuttuva nimi on ``None``, ei korvike -- mutta näytettävä on aina jotain."""
    (team,) = build_teams(
        [
            TeamObservation(
                faction_id="faction-x",
                match_id="1-m0",
                observed_at=KICKOFF,
                roster=members("x", 5, 0),
            )
        ],
        min_common=MIN_COMMON,
    )

    assert team.name is None
    assert team.display_name == "faction-x"


# -- Silta arkistoon --------------------------------------------------------


def test_lineup_keys_are_attached_to_every_team_above_the_threshold(
    division: tuple[Team, ...],
) -> None:
    """Arkiston kokoonpanotiiviste liitetään joukkueeseen rosterin perusteella.

    Tiiviste ei ole identiteetti -- yksi vaihto muuttaa sen -- mutta se on ainoa
    silta ``aggregates/<team_key>``-hakemistoihin, joita tämä tarina ei nimeä
    uudelleen.
    """
    rcave = find_teams(division, "Rcave").team
    lineup = set(sorted(rcave.player_ids)[:5])

    updated, contested = assign_lineup_keys(division, {"ff03fb54599d3311": lineup}, 3)

    by_key = {team.team_key: team for team in updated}
    assert by_key[rcave.team_key].lineup_keys == ("ff03fb54599d3311",)
    assert contested == ()
    assert all(
        not team.lineup_keys for key, team in by_key.items() if key != rcave.team_key
    )


def test_a_lineup_below_the_threshold_is_attached_to_nobody(
    division: tuple[Team, ...],
) -> None:
    """Kaksi yhteistä pelaajaa ei ole joukkue vaan sattuma."""
    rcave = find_teams(division, "Rcave").team
    lineup = set(sorted(rcave.player_ids)[:2])

    updated, contested = assign_lineup_keys(division, {"jokinlineup": lineup}, 3)

    assert all(not team.lineup_keys for team in updated)
    assert contested == ()


def test_a_lineup_claimed_by_two_teams_is_given_to_both_and_flagged() -> None:
    """Katselmus: "eniten voittaa" tarkoitti eri asiaa kuin ``aggregate``issa.

    Sama asetusarvo (``team_identity_min_common``) merkitsi kahdessa paikassa
    kahta eri asiaa: ``aggregate`` liittää **kaikki** kynnyksen ylittävät, tämä
    liitti vain parhaan. Nyt sääntö on sama, ja kiista on merkitty -- jotta
    jatkovaihe ei laskisi tiivistettä kahdesti tietämättä tekevänsä niin.
    """
    shared = members("yhteiset", 4, offset=0)
    a = build_teams(
        [
            TeamObservation(
                faction_id="A",
                match_id="1-a",
                observed_at=KICKOFF,
                name="A",
                roster=shared + members("aaa", 2, offset=100)[:1],
            ),
            TeamObservation(
                faction_id="B",
                match_id="1-b",
                observed_at=KICKOFF,
                name="B",
                roster=shared + members("bbb", 2, offset=200)[:1],
            ),
        ],
        # Kynnys 5, jottei liittäminen tee näistä samaa joukkuetta -- tässä
        # mitataan kokoonpanoliitosta eikä identiteettiä.
        min_common=5,
    )
    lineup = {member.game_player_id for member in shared}

    updated, contested = assign_lineup_keys(a, {"yhteinen": lineup}, 3)

    assert len(a) == 2
    assert all(team.lineup_keys == ("yhteinen",) for team in updated)
    assert contested == ("yhteinen",)


def test_assigning_lineups_keeps_the_teams_otherwise_untouched(
    division: tuple[Team, ...],
) -> None:
    updated, contested = assign_lineup_keys(division, {}, 3)

    assert [t.team_key for t in updated] == [t.team_key for t in division]
    assert all(not team.lineup_keys for team in updated)
    assert contested == ()
