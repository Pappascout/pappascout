"""``domain.selection`` -- rosterikynnyksen testit (Story 3.3).

Moduuli on puhdas, joten yksikään testi tässä tiedostossa ei koske verkkoon
eikä levylle: pelaajajoukot rakennetaan käsin. Tunnisteet ovat oikean muotoisia
SteamID64:iä (``test_teams.steam_id``), koska kynnys on joukko-operaatio niiden
välillä -- keksitty ``"a"`` läpäisisi testin muttei kertoisi mitään siitä, mitä
oikeassa aineistossa tapahtuu.

Kuusi asiaa lukitaan täällä:

* **Kynnys on joukko-operaatio.** 5/5 kelpaa, 4/5 kelpaa, 3/5 ei -- ja
  ulkopuolinen lasketaan mukaan otantaan, ei pois.
* **Rivin invariantit ovat rakenteessa.** Tyhjä syy, hylkäys luokan kanssa,
  hyväksyntä ilman luokkaa ja tuntematon lähde ovat kaikki mahdottomia
  rakentaa.
* **Lähde sanotaan ääneen.** Parsittu demo on havainto, parsimaton ennuste.
* **Havainto voittaa, ero kerrotaan** -- mutta eroa ei väitetä silloin kun
  vertailtavaa ei ole.
* **Luokka ja lukusuhde eivät saa väittää eri asiaa.** Neljä vakipelaajaa ilman
  ulkopuolista ja kuusi vakipelaajaa ovat molemmat tapauksia, joissa luokan
  nimittäjä eroaa kokoonpanon koosta -- ja rivi sanoo sen.
* **Vetotiedon kartta ei ole todiste pelatusta kartasta.**
"""

from __future__ import annotations

import pytest
from test_teams import steam_id

from pappascout.constants import ROSTER_CLASSES
from pappascout.domain.selection import (
    ROSTER_SOURCE_FI,
    ROSTER_SOURCES,
    MapCandidate,
    MapSelection,
    class_labels,
    counts,
    evaluate,
    guaranteed_maps,
    map_demo_id,
    select_maps,
    sort_key,
)
from pappascout.errors import SettingsError

#: ``[thresholds]``-osion mitatut oletukset (settings.toml:464-473).
ROSTER_SIZE = 5
MIN_REGULARS = 4

#: Seitsemän pelaajan vakirosteri, sama koko kuin Rcave Veteransilla (mitattu).
REGULARS = tuple(steam_id(index) for index in range(1, 8))
OUTSIDER = steam_id(90)
SECOND_OUTSIDER = steam_id(91)

MATCH = "1-f6a06dc8-5c26-4238-b57a-6b357043a5af"

NAMES = {
    OUTSIDER: "vieras",
    SECOND_OUTSIDER: "toinen_vieras",
    REGULARS[0]: "SSStttNNN",
}


def candidate(
    on_map: tuple[str, ...],
    *,
    index: int = 0,
    observed: tuple[str, ...] | None = None,
    is_league: bool = True,
    map_name: str = "de_ancient",
    certainly_played: bool = True,
    observation_note: str | None = None,
) -> MapCandidate:
    """Yksi kartta: ottelurosteri ennusteeksi, demon kokoonpano havainnoksi."""
    return MapCandidate(
        map_demo_id=map_demo_id(MATCH, index),
        match_id=MATCH,
        map_index=index,
        map_name=map_name,
        is_league=is_league,
        certainly_played=certainly_played,
        match_roster=frozenset(on_map),
        observed_players=None if observed is None else frozenset(observed),
        observation_note=observation_note,
    )


def decide(
    candidate_: MapCandidate, roster: tuple[str, ...] = REGULARS
) -> MapSelection:
    return evaluate(
        candidate_,
        roster=frozenset(roster),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
        names=NAMES,
    )


def row(**overrides) -> dict:
    """Kelvollisen rivin kentät, jotta invarianttitesti muuttaa vain yhtä."""
    base = {
        "map_demo_id": "1-x-0",
        "match_id": "1-x",
        "map_index": 0,
        "map_name": None,
        "is_league": False,
        "roster_ok": True,
        "roster_reason": "Kelpaa.",
        "roster_class": "5/5",
        "roster_source": "predicted",
    }
    base.update(overrides)
    return base


# -- Kynnys joukko-operaationa ----------------------------------------------


def test_five_regulars_is_accepted_as_the_full_class() -> None:
    """I/O-matriisi: 5 vakipelaajaa -> kelpaa, luokka 5/5."""
    decided = decide(candidate(REGULARS[:5]))

    assert decided.roster_ok is True
    assert decided.roster_class == "5/5"
    assert decided.outsiders == ()
    assert len(decided.regulars) == 5


def test_four_regulars_and_one_outsider_is_accepted() -> None:
    """Veeti 2026-09-04: ottelu on samaa joukkuetta vastaan vaikka yksi olisi sub.

    Ulkopuolinen **lasketaan mukaan** -- ero on luokassa, ei siinä kuka on
    otannassa.
    """
    decided = decide(candidate(REGULARS[:4] + (OUTSIDER,)))

    assert decided.roster_ok is True
    assert decided.roster_class == "4/5"
    assert decided.outsiders == (OUTSIDER,)
    assert decided.players_seen == 5


def test_three_regulars_is_rejected_and_the_reason_names_the_threshold() -> None:
    """I/O-matriisi: 3 vakipelaajaa -> ei kelpaa, syy kertoo luvut ja kynnyksen."""
    decided = decide(candidate(REGULARS[:3] + (OUTSIDER, SECOND_OUTSIDER)))

    assert decided.roster_ok is False
    assert decided.roster_class is None
    assert "3/5" in decided.roster_reason
    assert "4/5" in decided.roster_reason


def test_no_rejection_is_ever_without_a_reason() -> None:
    """Frozen-sääntö: ``roster_ok = false`` ilman syytä on kielletty."""
    rows = select_maps(
        [
            candidate(REGULARS[:5], index=0),
            candidate(REGULARS[:3] + (OUTSIDER, SECOND_OUTSIDER), index=1),
            candidate((), index=2),
        ],
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )

    assert len(rows) == 3
    for decided in rows:
        assert decided.roster_reason.strip()


# -- Rivin invariantit ovat rakenteessa -------------------------------------


def test_a_row_without_a_reason_cannot_be_built_at_all() -> None:
    with pytest.raises(ValueError, match="ei ole syytä"):
        MapSelection(**row(roster_reason="   "))


def test_a_rejected_row_cannot_carry_a_class() -> None:
    """Luokka hylätyllä rivillä olisi väite kierroksista, joita ei lasketa."""
    with pytest.raises(ValueError, match="rosteriluokka"):
        MapSelection(**row(roster_ok=False, roster_class="4/5"))


def test_an_accepted_row_cannot_be_missing_its_class() -> None:
    """Sellainen rivi olisi otannassa muttei kummassakaan luokkalaskurissa.

    Ilman tätä väitettä ``accepted != class_5/5 + class_4/5`` olisi mahdollista
    ilman että mikään huutaa -- ja juuri sen invariantin :func:`counts` lupaa.
    """
    with pytest.raises(ValueError, match="rosteriluokkaa"):
        MapSelection(**row(roster_ok=True, roster_class=None))


def test_an_unknown_source_cannot_be_built() -> None:
    """``Literal`` on tarkistus tyyppitarkistimelle, ei ajossa.

    Ilman vartijaa kelvoton arvo rakentuisi ja räjähtäisi vasta ``source_fi``n
    KeyErrorina jossain aivan muualla.
    """
    with pytest.raises(ValueError, match="lähde on"):
        MapSelection(**row(roster_source="guessed"))


def test_the_counts_invariants_hold_for_every_row() -> None:
    rows = select_maps(
        [
            candidate(REGULARS[:5], index=0),
            candidate(REGULARS[:4] + (OUTSIDER,), index=1),
            candidate(REGULARS[:2], index=2),
        ],
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )
    numbers = counts(rows)

    assert numbers["accepted"] + numbers["rejected"] == numbers["map_demos"]
    assert (
        sum(numbers[f"class_{label}"] for label in ROSTER_CLASSES)
        == numbers["accepted"]
    )


# -- Syy on luettava ja nimeää ihmiset ---------------------------------------


def test_the_reason_names_the_outsider_by_nickname() -> None:
    """I/O-matriisi: syy kertoo **kuka** oli ulkopuolinen, ei vain montako."""
    decided = decide(candidate(REGULARS[:4] + (OUTSIDER,)))

    assert "vieras" in decided.roster_reason
    assert OUTSIDER not in decided.roster_reason


def test_an_unnamed_outsider_falls_back_to_the_identifier() -> None:
    """Nimimerkki voi puuttua; keksitty nimi osoittaisi väärään pelaajaan."""
    unknown = steam_id(555)
    decided = decide(candidate(REGULARS[:4] + (unknown,)))

    assert unknown in decided.roster_reason


def test_an_observation_note_is_carried_into_the_reason() -> None:
    """Rikkinäinen kokoonpanotaulu ei saa kadota jäljettömästi.

    Ilman tätä ketjua havainto demottuisi ennusteeksi hiljaa: rivi näyttäisi
    tavalliselta ennusteelta, vaikka demo on olemassa ja rikki.
    """
    note = "Huom: demon kokoonpanotaulu on olemassa muttei luettavissa."
    decided = decide(candidate(REGULARS[:5], observation_note=note))

    assert note in decided.roster_reason
    assert decided.roster_source == "predicted"


def test_an_unknown_roster_is_its_own_reason_not_a_failed_threshold() -> None:
    """Ilman vakirosteria kynnyksen alitus ei ole tosi väite."""
    decided = decide(candidate(REGULARS[:5]), roster=())

    assert decided.roster_ok is False
    assert "vakirosteria ei tiedetä" in decided.roster_reason
    assert "kynnys on" not in decided.roster_reason


def test_a_map_without_any_known_players_is_rejected() -> None:
    """Tyhjä ottelurosteri ei ole kynnyksen alitus vaan tuntematon kokoonpano."""
    decided = decide(candidate(()))

    assert decided.roster_ok is False
    assert decided.roster_class is None
    assert "ei tiedetä" in decided.roster_reason


def test_a_parsed_demo_without_this_team_is_rejected_with_its_own_reason() -> None:
    """Havaittu tyhjä on eri asia kuin "ei tiedossa", ja syy sanoo sen."""
    decided = decide(candidate(REGULARS[:5], observed=()))

    assert decided.roster_ok is False
    assert decided.roster_source == "observed"
    assert "Demo on parsittu" in decided.roster_reason


# -- Luokka ja lukusuhde eivät väitä eri asiaa -------------------------------


def test_four_regulars_and_nobody_else_says_there_was_no_outsider() -> None:
    """Luokka ``4/5`` väittäisi yksin ulkopuolista, jota ei ole.

    Neljän pelaajan kokoonpanossa neljä vakipelaajaa täyttää kynnyksen, mutta
    viides paikka on tyhjä eikä vieraan. Rivi sanoo molemmat.
    """
    decided = decide(candidate(REGULARS[:4]))

    assert decided.roster_ok is True
    assert decided.roster_class == "4/5"
    assert decided.outsiders == ()
    assert "Ulkopuolisia ei ollut" in decided.roster_reason
    assert "4 pelaajaa odotetun 5 sijaan" in decided.roster_reason


def test_six_regulars_is_the_full_class_and_the_reason_admits_the_size() -> None:
    """Ilman kattoa luokka olisi 5/5 ja syy sanoisi "6/6" -- eri nimittäjät."""
    decided = decide(candidate(REGULARS[:6]))

    assert decided.roster_class == "5/5"
    assert decided.players_seen == 6
    assert "6/6" in decided.roster_reason
    assert "6 pelaajaa odotetun 5 sijaan" in decided.roster_reason


def test_a_full_five_player_lineup_says_nothing_about_the_size() -> None:
    """Huomautus on poikkeamaa varten; normaalitilanne ei tarvitse sitä."""
    decided = decide(candidate(REGULARS[:5]))

    assert "odotetun" not in decided.roster_reason


# -- Ennuste vs. havainto ----------------------------------------------------


def test_an_unparsed_map_is_a_prediction_and_says_so() -> None:
    """I/O-matriisi: demoa ei ole parsittu -> luokka on ennuste ottelurosterista."""
    decided = decide(candidate(REGULARS[:5]))

    assert decided.roster_source == "predicted"
    assert decided.source_fi == "ennuste"
    assert "ennuste" in decided.roster_reason


def test_a_parsed_map_is_an_observation_and_says_so() -> None:
    """I/O-matriisi: ``lineups.parquet`` olemassa -> luokka on havainto."""
    decided = decide(candidate(REGULARS[:5], observed=REGULARS[:5]))

    assert decided.roster_source == "observed"
    assert decided.source_fi == "havainto"
    assert "havainto" in decided.roster_reason


def test_the_observation_wins_over_the_match_roster() -> None:
    """I/O-matriisi: parsittu kokoonpano eroaa ottelurosterista -> havainto voittaa.

    Ottelurosterissa on viisi vakipelaajaa, demossa neljä ja yksi ulkopuolinen:
    vaihto karttojen välissä. Luokka on **4/5**, ei 5/5.
    """
    decided = decide(candidate(REGULARS[:5], observed=REGULARS[:4] + (OUTSIDER,)))

    assert decided.roster_class == "4/5"
    assert decided.roster_source == "observed"
    assert decided.outsiders == (OUTSIDER,)


def test_the_difference_to_the_match_roster_is_told_not_silenced() -> None:
    """I/O-matriisi: ero kerrotaan -- vaihto on juuri se, mitä varten kynnys on."""
    decided = decide(candidate(REGULARS[:5], observed=REGULARS[:4] + (OUTSIDER,)))

    assert decided.drifted is True
    assert decided.joined == (OUTSIDER,)
    assert decided.left == (REGULARS[4],)
    assert "eroaa ottelurosterista" in decided.roster_reason
    assert "vieras" in decided.roster_reason


def test_no_difference_is_claimed_when_there_is_no_match_roster() -> None:
    """Ilman ottelurosteria koko havainto olisi "ero", ja se olisi väärä väite."""
    decided = evaluate(
        MapCandidate(
            map_demo_id=map_demo_id(MATCH, 0),
            match_id=MATCH,
            map_index=0,
            observed_players=frozenset(REGULARS[:5]),
        ),
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )

    assert decided.drifted is False
    assert decided.joined == ()
    assert decided.left == ()


def test_no_difference_is_claimed_when_the_observation_is_empty() -> None:
    """Rivi ei saa kertoa vaihdosta kartalla, jonka kokoonpano on tuntematon.

    Tyhjä havainto tuottaisi vertailussa ``left`` = koko ottelurosteri, ja
    tuloste kertoisi "Vaihto karttojen välissä" kartalle, jonka oma syy sanoo
    ettei kokoonpanoa tiedetä. Kaksi väitettä, joista toinen on väärä.
    """
    decided = decide(candidate(REGULARS[:5], observed=()))

    assert decided.drifted is False
    assert decided.left == ()
    assert "eroaa ottelurosterista" not in decided.roster_reason


def test_an_identical_observation_reports_no_difference() -> None:
    decided = decide(candidate(REGULARS[:5], observed=REGULARS[:5]))

    assert decided.drifted is False
    assert "eroaa" not in decided.roster_reason


# -- Vetotiedon kartta ei ole todiste pelatusta kartasta ---------------------


@pytest.mark.parametrize(
    "best_of,expected",
    [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (None, None), (0, None)],
)
def test_the_guaranteed_map_count_comes_from_the_match_length(
    best_of: int | None, expected: int | None
) -> None:
    """BO2 on kokonaan varma, BO3 vain kahden kartan osalta."""
    assert guaranteed_maps(best_of) == expected


def test_a_map_that_may_not_have_been_played_is_kept_out_of_the_sample() -> None:
    """2-0 päättyneessä BO3:ssa vedossa on kolme karttaa mutta demoja kaksi.

    Ilman tätä sääntöä kolmas kartta olisi otannassa täytenä 5/5-karttana,
    vaikka sitä ei ehkä pelattu lainkaan.
    """
    decided = decide(candidate(REGULARS[:5], index=2, certainly_played=False))

    assert decided.roster_ok is False
    assert decided.roster_class is None
    assert decided.certainly_played is False
    assert "ottelun pituus" in decided.roster_reason


def test_a_parsed_demo_proves_the_map_was_played() -> None:
    """Demoa ei ole olemassa kartasta, jota ei pelattu -- havainto on todiste."""
    decided = decide(
        candidate(REGULARS[:5], index=2, certainly_played=False, observed=REGULARS[:5])
    )

    assert decided.roster_ok is True
    assert decided.roster_class == "5/5"
    assert decided.roster_source == "observed"


def test_uncertain_maps_are_counted_separately() -> None:
    rows = select_maps(
        [
            candidate(REGULARS[:5], index=0),
            candidate(REGULARS[:5], index=1),
            candidate(REGULARS[:5], index=2, certainly_played=False),
        ],
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )

    numbers = counts(rows)
    assert numbers["uncertain"] == 1
    assert numbers["accepted"] == 2


# -- is_league kulkee läpi muuttumatta --------------------------------------


def test_is_league_is_carried_through_unchanged_in_both_directions() -> None:
    """Päätöksen tekee vaihe ``competition_id``:stä; domain ei arvaa kumpaakaan."""
    league = decide(candidate(REGULARS[:5], is_league=True))
    other = decide(candidate(REGULARS[:5], is_league=False))

    assert league.is_league is True
    assert other.is_league is False


# -- Tunniste ---------------------------------------------------------------


def test_the_unit_identifier_is_match_id_and_zero_based_map_index() -> None:
    assert map_demo_id(MATCH, 0) == f"{MATCH}-0"
    assert map_demo_id(MATCH, 1) == f"{MATCH}-1"


@pytest.mark.parametrize("match,index", [("", 0), (MATCH, -1)])
def test_an_impossible_unit_identifier_is_refused(match: str, index: int) -> None:
    """Hiljainen ``-1``-pääte osoittaisi tiedostoon, jota ei ole."""
    with pytest.raises(ValueError):
        map_demo_id(match, index)


# -- Luokkien nimet johdetaan kynnyksistä ------------------------------------


def test_the_class_labels_come_from_the_thresholds() -> None:
    assert class_labels(ROSTER_SIZE, MIN_REGULARS) == ("5/5", "4/5")
    assert set(class_labels(ROSTER_SIZE, MIN_REGULARS)) <= set(ROSTER_CLASSES)


def test_a_threshold_that_would_invent_a_class_is_a_settings_error() -> None:
    """``roster_size = 6`` on kelvollinen PositiveInt mutta väärä asetus.

    **Asetusvirhe eikä ohjelmavirhe**: paljas ``ValueError`` päätyisi
    komentorivillä muotoon "Odottamaton virhe -- ohjelmavirhe", exit 2, vaikka
    korjaus on käyttäjän omassa settings.tomlissa.
    """
    with pytest.raises(SettingsError, match="tunnettujen luokkien"):
        class_labels(6, 5)


@pytest.mark.parametrize("size,minimum", [(0, 0), (5, 0), (5, 6), (-1, 1)])
def test_impossible_thresholds_are_settings_errors(size: int, minimum: int) -> None:
    with pytest.raises(SettingsError, match="settings.tomlissa"):
        class_labels(size, minimum)


# -- Järjestys ja yhteenvetoluvut -------------------------------------------


def test_rows_sort_by_match_and_then_map_index() -> None:
    """Diffattavuus: kahden ajon ero on luettava vain vakaassa järjestyksessä."""
    rows = [
        decide(candidate(REGULARS[:5], index=1)),
        decide(candidate(REGULARS[:5], index=0)),
    ]

    assert [r.map_index for r in sorted(rows, key=sort_key)] == [0, 1]


def test_the_counts_are_computed_from_the_rows_themselves() -> None:
    """Tiedosto ja yhteenveto eivät voi kertoa eri asiaa, koska lähde on sama."""
    rows = select_maps(
        [
            candidate(REGULARS[:5], index=0),
            candidate(REGULARS[:4] + (OUTSIDER,), index=1, is_league=False),
            candidate(REGULARS[:3] + (OUTSIDER, SECOND_OUTSIDER), index=2),
            candidate(REGULARS[:5], index=3, observed=REGULARS[:5]),
        ],
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )

    numbers = counts(rows)

    assert numbers["map_demos"] == 4
    assert numbers["accepted"] == 3
    assert numbers["rejected"] == 1
    assert numbers["league"] == 3
    assert numbers["observed"] == 1
    assert numbers["predicted"] == 3
    assert numbers["class_5/5"] == 2
    assert numbers["class_4/5"] == 1


def test_the_source_names_are_finnish_and_cover_every_source() -> None:
    assert set(ROSTER_SOURCE_FI) == set(ROSTER_SOURCES)
    assert ROSTER_SOURCE_FI["observed"] == "havainto"
    assert ROSTER_SOURCE_FI["predicted"] == "ennuste"
