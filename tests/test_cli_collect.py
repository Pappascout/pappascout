"""``pappascout collect`` -- komennon testit (Story 3.5).

Keräys on ``fetch``in sisar: **sama lataus, toinen yksikkövalinta**. Siksi
tämä tiedosto ei toista latauksen sääntöjä (ne ovat ``test_stage_fetch.py``ssä
ja ``test_cli_fetch.py``ssä) vaan lukitsee sen, mikä tässä komennossa on eri:

* **Yksiköt tulevat otteluindeksistä, eivät valintatiedostosta.** Koko
  divisioona, ei yhden joukkueen otanta -- juuri se ero on komennon koko syy.
* **Pelattu ottelu ei katoa hiljaa.** Tyhjä ``map_picks`` on oma lohkonsa
  syineen, eikä sitä lasketa pelaamattomaksi.
* **Indeksin ikä ja tuntematon ottelun pituus sanotaan ääneen.** Molemmat
  rajaavat sitä, mitä ajo näkee, eikä kumpaakaan saa jättää pääteltäväksi.
* **Ruudun luvut ovat levyltä laskettavissa.** Story 3.4:n opetus: luku, jota
  mikään ei vartioi, voi olla mitä tahansa eikä mikään huomaa.

Ketju ``discover`` -> ``collect`` ajetaan oikeilla vaiheilla feikkiporttien
takaa, ja **molemmat demohakemistomoodit** kulkevat jokaisen testin läpi.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import LOCAL_DEMOS_DIRNAME
from test_stage_discover import CHAMPIONSHIP, FakeSource, division_matches
from test_stage_fetch import DEMO_BYTES, FakeDemo, FakeDemoSource
from typer.testing import CliRunner

from pappascout.adapters.protocols import Match
from pappascout.cli import (
    EXIT_KNOWN_ERROR,
    MAX_LISTED_UNITS,
    NO_VETO_STALE_DAYS,
    _collect_no_veto,
    _render_collect_plan,
    app,
    main,
)
from pappascout.domain.models import SETTINGS_ENV_VAR, load_settings
from pappascout.stages import archive_paths
from pappascout.stages import discover as discover_stage
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import select as select_stage

runner = CliRunner()

#: Oma joukkue asetustiedostossa; käytetään vain vertailussa ``select``iin.
SUBJECT = "Potku"


#: Pelattu ottelu, jonka ``status`` **ei ole** merkkijono ``"FINISHED"``.
#:
#: ``discover._is_played`` normalisoi kirjainkoon (``status.upper() in
#: PLAYED_STATUSES``), joten pienellä kirjoitettu tila on pelattu ottelu --
#: mutta naiivi vertailu ``status == "FINISHED"`` sanoisi toisin. Ilman tätä
#: riviä aineistossa ei olisi yhtäkään ottelua, jossa ``played`` ja tuo vertailu
#: eroavat, ja ``test_the_filter_is_played_not_the_status_string`` vartioisi
#: eroa, jota komentotestit eivät koskaan aja.
LOWERCASE_STATUS = "finished"


def with_a_lowercase_played_status(
    matches: tuple[Match, ...],
) -> tuple[Match, ...]:
    """Muuta **viimeisen** pelatun ottelun tila pieniksi kirjaimiksi.

    Viimeinen eikä ensimmäinen: ensimmäinen ottelu on monessa testissä se, jota
    muokataan erikseen, eikä kahden muutoksen pidä osua samaan riviin.
    """
    rows = list(matches)
    for index in reversed(range(len(rows))):
        if rows[index].status == "FINISHED":
            rows[index] = rows[index].__class__(
                **{**rows[index].__dict__, "status": LOWERCASE_STATUS}
            )
            return tuple(rows)
    raise AssertionError("aineistossa ei ole pelattuja otteluita")


def division_units(archive) -> tuple[str, ...]:
    """Odotetut tunnisteet **otteluindeksin ``played``-kentästä**.

    Kaksi asiaa, jotka tämä funktio ei saa tehdä:

    **Ei kysytä ``plan_division``ilta.** Silloin testi vertaisi funktiota
    itseensä ja menisi läpi myös silloin, kun se pudottaa puolet otteluista.

    **Eikä suodateta ``status == "FINISHED"``illa.** Se on eri sääntö kuin
    tuotantokoodin ``played``, ja
    ``test_stage_fetch.py::test_the_filter_is_played_not_the_status_string`` on
    kirjoitettu nimenomaan todistamaan, etteivät ne ole sama asia. Odotus, joka
    rakennetaan säännöllä jonka toinen testi kieltää, osuu oikeaan vain niin
    kauan kuin aineistossa ei ole otteluita joissa ne eroavat -- ja
    :data:`LOWERCASE_STATUS` on juuri sellainen ottelu.

    Lähde on siis indeksin oma ``played``, sellaisena kuin ``discover`` sen
    kirjoitti: se on riippumaton siitä funktiosta, jota testataan.
    """
    document = json.loads(archive.matches_index().read_text(encoding="utf-8"))
    return tuple(
        f"{row['match_id']}-{index}"
        for row in document["matches"]
        if row["played"]
        for index in range(len(row["map_picks"]))
    )


class Division:
    """Arkisto, jossa on ajettu oikea ``discover``, ja feikattu demoportti."""

    def __init__(self, archive, settings, source: FakeDemoSource) -> None:
        self.archive = archive
        self.settings = settings
        self.source = source
        self.matches: tuple[Match, ...] = ()
        self.units: tuple[str, ...] = ()

    def discover(
        self,
        matches: tuple[Match, ...] | None = None,
        *,
        lowercase: bool = True,
    ) -> tuple[str, ...]:
        """Kirjoita otteluindeksi näistä otteluista ja johdota niiden demot.

        Oletusaineistoon lisätään aina ottelu, jossa ``played`` ja
        ``status == "FINISHED"`` eroavat (:func:`with_a_lowercase_played_status`)
        -- muuten kaksi eri suodatinta osuisi yhteen jokaisessa komentotestissä.
        """
        base = division_matches() if matches is None else matches
        self.matches = with_a_lowercase_played_status(base) if lowercase else base
        discover_stage.run(
            self.settings.league,
            self.archive,
            None,
            source=FakeSource({CHAMPIONSHIP: self.matches}),
            thresholds=self.settings.thresholds,
        )
        self.units = division_units(self.archive)
        self.source.demos = {unit: FakeDemo(DEMO_BYTES) for unit in self.units}
        return self.units

    def played_ids(self) -> list[str]:
        """Indeksin mukaan pelatut ottelut -- ``played``, ei ``status``."""
        document = json.loads(
            self.archive.matches_index().read_text(encoding="utf-8")
        )
        return [r["match_id"] for r in document["matches"] if r["played"]]

    def unplayed_ids(self) -> list[str]:
        document = json.loads(
            self.archive.matches_index().read_text(encoding="utf-8")
        )
        return [r["match_id"] for r in document["matches"] if not r["played"]]

    def on_disk(self) -> list[str]:
        """Demot, jotka **oikeasti** ovat levyllä, tunnisteina."""
        return sorted(
            path.name[: -len(".dem.zst")]
            for directory in self.archive.demo_dirs()
            if directory.is_dir()
            for path in directory.glob("*.dem.zst")
        )


@pytest.fixture(params=["arkisto", "paikallinen"])
def division(request, settings_file: Path, tmp_path: Path, monkeypatch) -> Division:
    """Oikeat vaiheet, feikatut portit, molemmat demohakemistomoodit.

    Sama perustelu kuin ``test_cli_fetch.py``n ``pipeline``-fixturella: moodi,
    jota komentotestit eivät aja, kulkee CLI:n läpi nolla kertaa.
    """
    if request.param == "paikallinen":
        text = settings_file.read_text(encoding="utf-8")
        line = next(r for r in text.splitlines() if r.startswith("# demos_root = "))
        settings_file.write_text(
            text.replace(line, f"demos_root = '{tmp_path / 'paikalliset'}'", 1),
            encoding="utf-8",
        )
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    settings = load_settings()
    archive = archive_paths(settings.project)
    source = FakeDemoSource({})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda _s, _a: source
    )
    # Levy ei saa olla testin muuttuja: tarkistus on oma testinsä.
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    world = Division(archive, settings, source)
    world.discover()
    return world


def played_matches(**changes) -> tuple[Match, ...]:
    """Divisioonan ottelut, joista **pelatut** on muutettu annetuilla kentillä.

    Tämä on aineiston rakentaja eikä odotus, joten se saa katsoa tilaa -- mutta
    se katsoo **molempia** kirjoitusasuja, jottei pienellä kirjoitettu ottelu
    jäisi muuttamatta ja tuottaisi vahingossa eri lopputulosta kuin muut.
    """
    return tuple(
        (
            match.__class__(**{**match.__dict__, **changes})
            if (match.status or "").upper() == "FINISHED"
            else match
        )
        for match in division_matches()
    )


# -- Yksiköt tulevat otteluindeksistä ----------------------------------------


def test_the_whole_division_is_planned_and_downloaded(division) -> None:
    result = runner.invoke(app, ["collect"], input="k\n")

    assert result.exit_code == 0, result.output
    # **Koko luku, ei osajono.** Yksinumeroinen luku osuisi tunnisteeseen ja
    # ajoaikaan, ja väite menisi läpi myös väärällä luvulla ruudulla.
    assert f"{len(division.units)} demoa" in result.output
    assert "Ladataanko nämä demot?" in result.output
    assert division.source.asked == list(division.units)
    for unit in division.units:
        assert division.archive.find_demo(unit) is not None


def test_collect_reaches_matches_that_no_selection_file_contains(
    division,
) -> None:
    """**Komennon koko syy.** ``fetch`` hakee otannan, ``collect`` divisioonan.

    Jos keräys jäisi joukkuekohtaiseksi, se olisi ``fetch --team`` toisella
    nimellä -- ja kauden lopussa juuri ne ottelut, joita kukaan ei ole
    valinnut, katoaisivat FACEITin 30 päivän säilytykseen.
    """
    select_stage.run(
        division.settings.league,
        division.archive,
        SUBJECT,
        thresholds=division.settings.thresholds,
    )
    document = json.loads(
        next(division.archive.resolve("index/selections").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    otanta = {r["map_demo_id"] for r in document["selections"] if r["roster_ok"]}
    assert otanta, "aineisto ei tuottanut yhtäkään valittua karttaa"

    runner.invoke(app, ["collect", "--kylla"])

    haetut = set(division.source.asked)
    assert otanta < haetut, "keräyksen on oltava otantaa laajempi"
    assert haetut == set(division.units)


def test_an_unplayed_match_never_reaches_the_port(division) -> None:
    """Pelaamattoman ottelun demoa ei ole: kysyminen kuluttaisi kiintiötä."""
    runner.invoke(app, ["collect", "--kylla"])

    unplayed = division.unplayed_ids()
    assert unplayed, "aineistossa ei ole pelaamattomia otteluita"
    for match_id in unplayed:
        assert not any(unit.startswith(match_id) for unit in division.source.asked)


def test_own_team_is_not_filtered_out_of_the_division(division) -> None:
    """Divisioona tarkoittaa divisioonaa -- myös omat ottelut kerätään."""
    own = division.settings.project.own_team_name
    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    pelatut = set(division.played_ids())
    omat = [
        m.match_id
        for m in division.matches
        if m.match_id in pelatut and any(s.name == own for s in m.teams)
    ]
    assert omat, "aineistossa ei ole oman joukkueen pelattuja otteluita"
    for match_id in omat:
        assert f"{match_id}-0" in division.source.asked


# -- Vahvistuskysymys --------------------------------------------------------


def test_answering_no_downloads_nothing(division) -> None:
    result = runner.invoke(app, ["collect"], input="e\n")

    assert result.exit_code == 0, result.output
    assert "Peruttu. Yhtään demoa ei ladattu." in result.output
    assert division.source.asked == []
    assert division.on_disk() == []


def test_no_input_at_all_is_cancelled_in_finnish(division) -> None:
    result = runner.invoke(app, ["collect"], input="")

    assert result.exit_code == 0, result.output
    assert "Aborted" not in result.output
    assert "[y/N]" not in result.output
    assert "Peruttu. Yhtään demoa ei ladattu." in result.output
    assert division.source.asked == []


def test_kylla_skips_the_question_but_not_the_plan(division) -> None:
    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert "Ladataanko" not in result.output
    assert f"{len(division.units)} demoa" in result.output
    assert division.source.asked == list(division.units)


def test_the_target_directory_is_shown_before_the_question(division) -> None:
    result = runner.invoke(app, ["collect"], input="e\n")

    kohde = [r for r in result.output.splitlines() if r.strip().startswith("Kohde")]
    assert kohde, f"suunnitelmassa ei ole Kohde-riviä:\n{result.output}"
    assert str(division.archive.demos_dir()) in kohde[0]
    assert result.output.index("Kohde") < result.output.index("Ladataanko")


def test_the_shown_target_is_the_directory_that_is_written_to(division) -> None:
    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    written = division.archive.find_demo(division.units[0])
    assert written is not None
    kohde = [r for r in result.output.splitlines() if r.strip().startswith("Kohde")]
    assert kohde
    assert all(str(written.parent) in row for row in kohde)


# -- Turvallinen ajaa milloin tahansa ----------------------------------------


def test_a_second_run_downloads_nothing_and_says_so(division) -> None:
    first = runner.invoke(app, ["collect", "--kylla"])
    assert first.exit_code == 0, first.output
    division.source.asked.clear()

    second = runner.invoke(app, ["collect", "--kylla"])

    assert second.exit_code == 0, second.output
    assert division.source.asked == []
    assert "ei ladattavaa" in second.output
    # Eikä kysymystä esitetä, kun ladattavaa ei ole.
    assert "Ladataanko" not in second.output


def test_a_second_run_still_counts_the_demos_that_are_on_disk(division) -> None:
    """"Ei ladattavaa" ei saa näyttää siltä, että otanta olisi kutistunut."""
    runner.invoke(app, ["collect", "--kylla"])

    second = runner.invoke(app, ["collect", "--kylla"])

    assert f"joista {len(division.units)} on jo levyllä" in second.output


# -- Pelattu ottelu ilman vetotietoa -----------------------------------------


def test_a_played_match_without_map_picks_is_shown_with_its_reason(
    division,
) -> None:
    """**Ei nolla riviä.** Story 3.3:n katselmuksen löytämä vika, käännettynä."""
    matches = played_matches(map_picks=())
    division.discover(matches)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    pelatut = division.played_ids()
    assert f"Pelattu ottelu ilman vetotietoa ({len(pelatut)})" in result.output
    for match_id in pelatut:
        assert match_id in result.output
    # Eikä yhtäkään porttikutsua: tunnisteita ei voi muodostaa.
    assert division.source.asked == []
    assert "ei ladattavaa" in result.output


def test_a_match_without_map_picks_is_not_called_unplayed(division) -> None:
    """Väärä syy on huonompi kuin ei syytä: se on väite, joka ei ole totta."""
    division.discover(played_matches(map_picks=()))

    result = runner.invoke(app, ["collect", "--kylla"])

    lohko = result.output.split("Pelattu ottelu ilman vetotietoa", 1)[1]
    assert "ei pelattu" not in lohko.lower()
    assert "karttalistaa" in lohko
    # Aineiston ottelut ovat viikkoja vanhoja, joten neuvo on tuonti eikä
    # discoverin uudelleenajo -- ks. test_the_advice_branches_on_the_age.
    assert "uv run pappascout import" in lohko


def test_the_no_veto_block_is_shown_even_when_there_is_something_to_download(
    division,
) -> None:
    """Lohko ei saa kadota sen taakse, että muuta ladattavaa on."""
    matches = list(division_matches())
    rikki = matches[0]
    matches[0] = rikki.__class__(**{**rikki.__dict__, "map_picks": ()})
    division.discover(tuple(matches))

    result = runner.invoke(app, ["collect", "--kylla"])

    assert "Pelattu ottelu ilman vetotietoa (1)" in result.output
    assert rikki.match_id in result.output
    assert division.source.asked, "muut ottelut jäivät hakematta"


# -- Indeksin ikä ja tuntematon ottelun pituus -------------------------------


def test_the_plan_says_how_old_the_match_index_is(division) -> None:
    """Indeksi rajaa koko yksikköjoukon, joten sen ikä kuuluu kysymykseen."""
    document = json.loads(
        division.archive.matches_index().read_text(encoding="utf-8")
    )

    result = runner.invoke(app, ["collect"], input="e\n")

    assert "Otteluindeksi" in result.output
    assert document["generated_at"] in result.output


def test_a_missing_best_of_makes_the_output_say_the_length_is_unknown(
    division,
) -> None:
    """Mitattu 2026-09-06: kenttä puuttui arkiston koko indeksistä.

    Kartat luetaan silloin vetotiedosta -- ja jokainen niistä yritetään.
    """
    matches = played_matches(best_of=None)
    division.discover(matches)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert "tuntematon" in result.output
    assert "best_of" in result.output
    # Ja kartat luettiin silti vetotiedosta: jokainen yritettiin.
    assert division.source.asked == list(division.units)


def test_a_known_best_of_does_not_claim_the_length_is_unknown(division) -> None:
    """Rivi on väite, ei koriste: se ei saa näkyä kun pituus tiedetään."""
    result = runner.invoke(app, ["collect"], input="e\n")

    assert "Ottelun pituus" not in result.output


def test_a_bo3_that_ended_two_nil_still_tries_the_third_map(division) -> None:
    """Kolmas kartta on **odotettu** ``no_demo``, ei syy jättää yrittämättä.

    Ohitus maksaisi demon, jota ei kuukauden päästä saa enää mistään; yritys
    maksaa yhden kutsun. Eikä toistuva ``no_demo`` lopeta sarjaa
    (``IDENTICAL_FAILURE_LIMIT`` katsoo vain ``download_failed``ia), joten
    loput divisioonasta latautuvat.
    """
    matches = played_matches(
        best_of=3, map_picks=("de_ancient", "de_nuke", "de_mirage")
    )
    units = division.discover(matches)
    kolmannet = [u for u in units if u.endswith("-2")]
    assert len(kolmannet) >= 3, "aineisto ei todista mitään yhdellä ottelulla"
    for unit in kolmannet:
        del division.source.demos[unit]

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert division.source.asked == list(units)
    assert f"{len(kolmannet)} ei saatavilla" in result.output
    assert f"{len(units) - len(kolmannet)} haettu" in result.output


def test_the_same_fault_three_times_stops_the_series_and_every_unit_gets_a_row(
    division,
) -> None:
    """Toistuva vika lopettaa yrittämisen -- muttei hiljaa lyhennä luetteloa.

    Suunnitelma lupasi tietyn määrän yksiköitä, ja sitä lyhyempi yhteenveto
    jättäisi käyttäjän arvaamaan mihin loput katosivat.
    """
    from pappascout.errors import ApiError

    units = division.units
    division.source.demos = {
        unit: ApiError(
            "FACEIT ei hyväksynyt latauslinkkipyyntöä.",
            status_code=400,
            advice="Tarkista FACEIT_DOWNLOADS_TOKEN.",
        )
        for unit in units
    }

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    raja = fetch_stage.IDENTICAL_FAILURE_LIMIT
    assert division.source.asked == list(units[:raja])
    assert f"{len(units)} epäonnistui" in result.output
    for unit in units:
        assert unit in result.output
    assert "Ei yritetty" in result.output


def test_the_summary_separates_all_four_outcomes(division) -> None:
    """**Hyväksymiskriteeri.** Neljä lukua, neljä eri jatkoa.

    Haettu, jo levyllä, ei saatavilla ja epäonnistunut ovat neljä eri asiaa,
    eikä yksikään saa vuotaa toiseen.
    """
    from pappascout.errors import ApiError, DemoUnavailable

    units = division.units
    # Yksi on jo levyllä ennen ajoa: ensimmäinen ajo hakee sen.
    runner.invoke(app, ["collect", "--kylla"])
    for unit in units[1:]:
        demo = division.archive.find_demo(unit)
        assert demo is not None
        demo.unlink()
        (demo.parent / f"{unit}.meta.json").unlink()
    division.source.asked.clear()
    division.source.demos[units[1]] = DemoUnavailable("FACEIT poisti tallenteen.")
    division.source.demos[units[2]] = ApiError(
        "Yhteys katkesi.", status_code=503, advice="Aja komento uudelleen."
    )

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert f"Lataus valmis: {len(units) - 3} haettu" in result.output
    assert "1 oli jo levyllä" in result.output
    assert "1 ei saatavilla" in result.output
    assert "1 epäonnistui" in result.output
    assert "FACEIT poisti tallenteen." in result.output
    assert "-> Aja komento uudelleen." in result.output


# -- Vetotiedottoman rivin aikaleima ja neuvo (katselmus 6.9.) --------------


def no_veto_row(match_id: str = "1-aaa", **changes):
    """Yksi vetotiedoton rivi tulosteen testaamiseen."""
    from pappascout.stages import fetch as stage

    fields = {
        "match_id": match_id,
        "reason": "Ottelu on pelattu, mutta karttalistaa ei ole.",
        "finished_at": "2026-09-05T20:14:00+00:00",
    }
    fields.update(changes)
    return stage.NoVetoMatch(**fields)


def test_the_row_carries_the_time_the_match_ended() -> None:
    """Aikaleima ruudulla on väite, ja väite on vartioitava.

    Ilman tätä ``_collect_no_veto``in aikaleimaosan voi poistaa kokonaan ilman
    että yksikään testi kaatuu -- todistettu katselmuksessa 6.9.
    """
    text = "\n".join(_collect_no_veto((no_veto_row(),)))

    assert "1-aaa" in text
    assert "päättyi 2026-09-05T20:14:00+00:00" in text


def test_the_row_scales_with_the_timestamp_it_is_given() -> None:
    """Kiinteä teksti menisi läpi jokaisesta olemassaolotarkistuksesta."""
    eka = "\n".join(_collect_no_veto((no_veto_row(),)))
    toka = "\n".join(
        _collect_no_veto((no_veto_row(finished_at="2026-08-01T10:00:00+00:00"),))
    )

    assert "2026-09-05T20:14:00+00:00" in eka
    assert "2026-08-01T10:00:00+00:00" in toka
    assert eka != toka


def test_a_row_without_a_timestamp_does_not_print_the_word_none() -> None:
    """Puuttuva aikaleima jätetään pois, ei tulosteta ``None``ina.

    "(päättyi None)" olisi ruudulla oleva ohjelmointivirhe, ja käyttäjä joutuisi
    arvaamaan tarkoittaako se puuttuvaa tietoa vai jotain muuta.
    """
    text = "\n".join(_collect_no_veto((no_veto_row(finished_at=None),)))

    assert "1-aaa" in text
    assert "None" not in text
    assert "päättyi" not in text


def test_the_advice_branches_on_the_age_of_the_match() -> None:
    """**``finished_at``in dokumentoitu tarkoitus on tässä käytössä.**

    Tuoreelta ottelulta veto puuttuu, koska indeksi on ottelua vanhempi --
    ``discover`` korjaa sen. Viikkoja vanhalla ottelulla ``discover`` on jo
    ajettu ottelun jälkeen eikä vetoa silti ole, joten uudelleenajo ei tuota
    mitään. Yhteinen neuvo olisi oikea korkeintaan toiselle.
    """
    nyt = datetime.now(UTC)
    tuore = "\n".join(
        _collect_no_veto((no_veto_row(finished_at=nyt.isoformat()),))
    )
    vanha = "\n".join(
        _collect_no_veto(
            (
                no_veto_row(
                    finished_at=(
                        nyt - timedelta(days=NO_VETO_STALE_DAYS + 1)
                    ).isoformat()
                ),
            )
        )
    )

    assert "uv run pappascout discover" in tuore
    assert "import" not in tuore
    assert "uv run pappascout import" in vanha
    assert "discover ei todennäköisesti tuo sitä" in vanha


def test_an_unknown_age_gets_the_cheap_advice_not_the_expensive_one() -> None:
    """Tuntematon ikä ei ole "vanha": neuvoa ei valita tiedolla, jota ei ole."""
    text = "\n".join(_collect_no_veto((no_veto_row(finished_at=None),)))

    assert "uv run pappascout discover" in text
    assert "import" not in text


def test_an_unparseable_timestamp_does_not_crash_the_plan() -> None:
    """Rikkinäinen aikaleima on tuntematon ikä, ei poikkeus."""
    text = "\n".join(_collect_no_veto((no_veto_row(finished_at="eilen"),)))

    assert "uv run pappascout discover" in text


# -- Tyhjä divisioona ei valehtele (katselmus 6.9.) -------------------------


def test_an_empty_division_does_not_claim_everything_is_on_disk(
    division,
) -> None:
    """**Väärä väite datasta.** Mitään ei ole levyllä, koska mitään ei tunneta.

    Tilanne syntyy väärästä tai vieraasta ``championship_ids``ista, toisen
    divisioonan indeksistä ja kaudesta, jota ei ole vielä pelattu.
    """
    pelaamattomat = tuple(
        match.__class__(
            **{**match.__dict__, "status": "SCHEDULED", "map_picks": ()}
        )
        for match in division_matches()
    )
    division.discover(pelaamattomat, lowercase=False)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert "ei ladattavaa" in result.output
    assert "jo levyllä -- ei ladattavaa" not in result.output
    assert "ei ole yhtään pelattua ottelua" in result.output
    # **Kohta 3:** tunniste, jolla suodatettiin, on se mitä käyttäjän on
    # verrattava indeksiin -- eikä sitä voi tehdä näkemättä sitä.
    assert CHAMPIONSHIP in result.output
    assert "championship_ids" in result.output
    assert "uv run pappascout discover" in result.output
    assert division.source.asked == []


def test_the_empty_message_names_the_ids_it_filtered_with() -> None:
    """``league_ids`` on luettu kenttä, ei täytetty. Kaksi eri asetusta,
    kaksi eri viestiä -- muuten kenttä olisi koriste."""
    yksi = _render_collect_plan(
        fetch_stage.CollectPlan(league_ids=("aaa-111",)), None, "kohde"
    )
    kaksi = _render_collect_plan(
        fetch_stage.CollectPlan(league_ids=("aaa-111", "bbb-222")), None, "kohde"
    )

    assert "aaa-111" in yksi
    assert "bbb-222" not in yksi
    assert "aaa-111, bbb-222" in kaksi


def test_all_on_disk_and_nothing_known_are_different_messages() -> None:
    """Kaksi tyhjää suunnitelmaa, kaksi eri tilannetta, kaksi eri viestiä."""
    levylla = _render_collect_plan(
        fetch_stage.CollectPlan(
            league_ids=("aaa-111",), present=("a-0", "a-1"), matches_played=1
        ),
        None,
        "kohde",
    )
    tyhja = _render_collect_plan(
        fetch_stage.CollectPlan(league_ids=("aaa-111",)), None, "kohde"
    )

    assert "Kaikki divisioonan demot ovat jo levyllä" in levylla
    assert "ei ole yhtään pelattua ottelua" not in levylla
    assert "Kaikki divisioonan demot ovat jo levyllä" not in tyhja
    assert "ei ole yhtään pelattua ottelua" in tyhja


# -- Levytilavaroitus: mahtuu yksi, ei mahdu kaikki (katselmus 6.9.) --------


def test_a_plan_that_does_not_fit_warns_but_does_not_stop(
    division, monkeypatch
) -> None:
    """Osittainen keräys on parempi kuin ei keräystä.

    FACEIT poistaa demon noin 30 päivässä: se osa, joka ehditään hakea, on
    tallessa lopullisesti. Vaihe tarkistaa tilan erikseen jokaisen demon
    kohdalla, joten ajo pysähtyy itsestään oikeaan kohtaan.
    """
    yksi_mahtuu = (
        fetch_stage.DEMO_SIZE_ESTIMATE_BYTES + fetch_stage.DISK_RESERVE_BYTES
    )
    todo = fetch_stage.plan_division(division.archive, division.settings.league)
    assert todo.estimated_bytes > yksi_mahtuu, "aineisto ei todista mitään"
    vapaana = yksi_mahtuu + fetch_stage.DEMO_SIZE_ESTIMATE_BYTES
    assert vapaana < todo.estimated_bytes
    monkeypatch.setattr(fetch_stage, "free_space", lambda _a: vapaana)

    result = runner.invoke(app, ["collect"], input="e\n")

    assert result.exit_code == 0, result.output
    assert "HUOM" in result.output
    assert "ei mahdu levylle" in result.output
    # Varoitus, ei portti: kysymys esitetään silti.
    assert "Ladataanko" in result.output
    assert "Levytila ei riitä" not in result.output


def test_a_plan_that_fits_gets_no_warning(division) -> None:
    """Varoitus on väite, ei koriste: se ei saa näkyä kun kaikki mahtuu."""
    result = runner.invoke(app, ["collect"], input="e\n")

    assert "HUOM" not in result.output
    assert "ei mahdu levylle" not in result.output


def test_the_warning_says_how_many_of_the_plan_would_fit() -> None:
    """Luku on se, jonka perusteella käyttäjä päättää -- ei siis kiinteä."""
    pending = tuple(f"a-{i}" for i in range(100))
    todo = fetch_stage.CollectPlan(
        pending=pending,
        matches_played=50,
        estimated_bytes=100 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES,
    )
    vapaana = fetch_stage.DISK_RESERVE_BYTES + 3 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES

    text = _render_collect_plan(todo, vapaana, "kohde")

    assert "arviolta 3 demolle 100:sta" in text


def test_the_warning_never_promises_more_than_the_plan_holds() -> None:
    """Kolme demoa suunnitelmassa ja tilaa neljälle: luku ei saa olla neljä."""
    todo = fetch_stage.CollectPlan(
        pending=("a-0", "a-1", "a-2"),
        matches_played=2,
        # Arvio on suurempi kuin vapaa tila, joten varoitus laukeaa...
        estimated_bytes=fetch_stage.DISK_RESERVE_BYTES * 100,
    )
    vapaana = fetch_stage.DISK_RESERVE_BYTES + 9 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES

    text = _render_collect_plan(todo, vapaana, "kohde")

    assert "arviolta 3 demolle 3:sta" in text


# -- Pitkä luettelo katkaistaan (katselmus 6.9.) ----------------------------


def test_a_long_unit_list_is_truncated_and_says_how_many_are_hidden() -> None:
    """Koko divisioonalla luettelo on toistasataa riviä.

    Vierimässä ruudulta pois olisivat juuri ne rivit, joiden takia suunnitelma
    tulostetaan: kohde, vapaa tila, vetotiedottomat ottelut ja itse kysymys.
    """
    pending = tuple(f"a-{i}" for i in range(132))
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=pending, matches_played=66, estimated_bytes=1024**3
        ),
        None,
        "kohde",
    )

    rivit = [r for r in text.splitlines() if r.startswith("  a-")]
    assert len(rivit) == MAX_LISTED_UNITS
    assert f"(+{132 - MAX_LISTED_UNITS} muuta" in text
    # Ja "Ladataan"-rivi kertoo yhä koko luvun: katkaisu koskee luetteloa.
    assert "132 demoa" in text


def test_a_short_list_is_not_truncated_and_says_nothing_about_hiding() -> None:
    """Katkaisuviesti on väite, eikä sitä saa näyttää kun mitään ei katkaistu."""
    pending = tuple(f"a-{i}" for i in range(MAX_LISTED_UNITS))
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=pending, matches_played=10, estimated_bytes=1024**3
        ),
        None,
        "kohde",
    )

    for unit in pending:
        assert f"  {unit}" in text
    assert "muuta" not in text


def test_the_whole_division_still_fits_on_a_screen(division) -> None:
    """Nykyinen divisioona (12 demoa) luetellaan yhä kokonaan."""
    result = runner.invoke(app, ["collect"], input="e\n")

    assert len(division.units) <= MAX_LISTED_UNITS
    for unit in division.units:
        assert unit in result.output
    assert "muuta --" not in result.output


# -- Yksikkö ja monikko taipuvat (katselmus 6.9.) ---------------------------


def test_one_match_and_one_map_are_singular_in_finnish() -> None:
    """"1 pelattua ottelua, 1 karttaa" on väärin suomeksi.

    Yhden ottelun divisioona ei ole harvinaisuus: kauden ensimmäinen ajo osuu
    juuri siihen, eli virhe näkyisi juuri kun työkalu esitellään.
    """
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=1, estimated_bytes=1024**2
        ),
        None,
        "kohde",
    )

    assert "Divisioona: 1 pelattu ottelu, 1 kartta, josta 0 on jo " in text
    assert "pelattua ottelua" not in text
    assert "karttaa" not in text
    # Relatiivipronomini taipuu samalla luvulla: sisar korjattiin samalla
    # apurilla (``_of_which_fi``), joten sen on nakyttava myos taalla.
    assert "joista" not in text


def test_two_or_more_take_the_partitive() -> None:
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0", "a-1"), matches_played=2, estimated_bytes=1024**2
        ),
        None,
        "kohde",
    )

    assert "Divisioona: 2 pelattua ottelua, 2 karttaa, joista 0 on jo " in text


def test_zero_takes_the_partitive_too() -> None:
    """Nolla taipuu kuten monikko: "0 pelattua ottelua"."""
    text = _render_collect_plan(fetch_stage.CollectPlan(), None, "kohde")

    assert "0 pelattua ottelua, 0 karttaa, joista 0 on jo " in text


# -- Tuntematon pituus kertoo laajuutensa (katselmus 6.9.) ------------------


def test_the_unknown_length_line_says_how_many_matches_it_means(
    division,
) -> None:
    """Havainto ilman laajuutta ei ole tarkistettavissa."""
    kaikki = played_matches(best_of=None)
    division.discover(kaikki)
    pelatut = len(division.played_ids())

    result = runner.invoke(app, ["collect"], input="e\n")

    assert f"tuntematon {pelatut} ottelussa" in result.output


def test_the_unknown_length_count_scales_with_the_plan() -> None:
    """Kiinteä luku menisi läpi jokaisesta olemassaolotarkistuksesta."""
    yksi = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=1, best_of_unknown=("a",)
        ),
        None,
        "kohde",
    )
    monta = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=9, best_of_unknown=tuple("abcdefghi")
        ),
        None,
        "kohde",
    )

    assert "tuntematon 1 ottelussa" in yksi
    assert "tuntematon 9 ottelussa" in monta


def test_the_unknown_length_line_says_what_to_do_about_it() -> None:
    """Story 3.7 (kohta 11): havainto ilman neuvoa jattaa arvaamaan.

    Kentta ei puutu lahteesta vaan **vanhasta indeksista**: ``discover``
    kirjoittaa sen (``discover._match_row``), joten uudelleenajo korjaa rivin.
    Ilman tata lausetta rivi nayttaa vialta, jolle ei ole tehtavissa mitaan.
    """
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=1, best_of_unknown=("a",)
        ),
        None,
        "kohde",
    )

    assert "vanhasta indeksista" in text.replace("ä", "a").replace("ö", "o")
    assert "uv run pappascout discover" in text


def test_no_advice_line_appears_when_every_length_is_known() -> None:
    """Neuvo kuuluu havaintoon: ilman havaintoa se olisi kohinaa."""
    text = _render_collect_plan(
        fetch_stage.CollectPlan(pending=("a-0",), matches_played=1),
        None,
        "kohde",
    )

    assert "Ottelun pituus" not in text
    assert "uv run pappascout discover" not in text


# -- Portit: indeksi, levytila, Downloads-oikeus -----------------------------


def test_without_a_match_index_the_error_says_to_run_discover(
    settings_file, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("sys.argv", ["pappascout", "collect", "--kylla"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    text = "".join(capsys.readouterr())
    assert "discover" in text


def test_a_full_disk_stops_the_command_before_the_question(
    division, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 1024)
    monkeypatch.setattr("sys.argv", ["pappascout", "collect"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    assert "Levytila ei riitä" in captured.err + captured.out
    assert "Ladataanko" not in captured.out
    assert division.source.asked == []


@pytest.fixture
def denied(division, monkeypatch):
    """Oikea adapteri, joka saa 403:n signauskutsuun.

    Feikkiporttti ei voi tuottaa Downloads API:n 403:a, ja juuri se sauma oli
    se, jonka koko Story 3.4:n testisarja ohitti.
    """
    from test_faceit_demos import DOWNLOADS, FakeResponse, FakeSession, build

    session = FakeSession(sign=FakeResponse(403), download=FakeResponse(200))
    real_source = build(division.archive.root.parent, session)
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda _s, _a: real_source
    )
    assert real_source.downloads_base_url == DOWNLOADS
    return division, session


def test_the_plan_is_shown_in_full_before_the_first_403(
    denied, monkeypatch, capsys
) -> None:
    """Suunnitelma on oma tuotoksensa: se näkyy vaikka lataus ei alkaisi."""
    world, _session = denied
    monkeypatch.setattr("sys.argv", ["pappascout", "collect", "--kylla"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    text = "".join(capsys.readouterr())
    assert f"{len(world.units)} demoa" in text
    for unit in world.units:
        assert unit in text


def test_only_one_signing_call_is_made_before_the_run_stops(
    denied, monkeypatch, capsys
) -> None:
    """Kahdellatoista demolla tämä olisi ollut 12 tuomittua kutsua."""
    world, session = denied
    assert len(world.units) > 1
    monkeypatch.setattr("sys.argv", ["pappascout", "collect", "--kylla"])

    with pytest.raises(SystemExit):
        main()

    capsys.readouterr()
    assert len(session.posts) == 1


def test_the_denied_message_names_the_status_page(
    denied, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("sys.argv", ["pappascout", "collect", "--kylla"])

    with pytest.raises(SystemExit):
        main()

    text = "".join(capsys.readouterr())
    assert "The Downloads API is a separate authorisation" in text
    assert "downloads-api-application" in text
    assert "aja komento uudelleen" not in text.lower()


def test_the_production_port_is_wired_on_the_collect_path(
    division, monkeypatch
) -> None:
    """**Mutaatiotodistus 4.** Kytkentä ``default_source``iin on mitattava.

    Ilman tätä ``collect`` voisi rakentaa portin ohi asetuksista, ja ero
    näkyisi vasta ensimmäisessä oikeassa ajossa.
    """
    seen: list[tuple] = []
    port = division.source

    def spy(settings, archive):
        seen.append((settings, archive))
        return port

    monkeypatch.setattr("pappascout.stages.fetch.default_source", spy)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    settings, archive = seen[0]
    assert settings.league.championship_ids == [CHAMPIONSHIP]
    assert archive.demos_dir() == division.archive.demos_dir()


# -- Ruudun luvut ovat levyltä laskettavissa ---------------------------------


def test_the_summary_counts_match_the_files_that_are_on_disk(division) -> None:
    """Story 3.4:n opetus: kovakoodattu luku menisi läpi jokaisesta väitteestä."""
    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    levylla = division.on_disk()
    assert levylla == sorted(division.units)
    assert f"Lataus valmis: {len(levylla)} haettu" in result.output
    kirjoitettu = fetch_stage.size_fi(len(levylla) * len(DEMO_BYTES))
    assert kirjoitettu in result.output


def test_the_plan_line_says_the_real_count_and_the_real_size(division) -> None:
    """Suunnitelman luvut tulevat suunnitelmasta, eivät mistään muualta."""
    todo = fetch_stage.plan_division(division.archive, division.settings.league)

    text = _render_collect_plan(todo, 9_900_000_000, r"D:\demot")

    assert f"{len(division.units)} demoa" in text
    odotettu = fetch_stage.size_fi(
        len(division.units) * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES
    )
    assert odotettu in text
    assert r"D:\demot" in text
    assert "9,2 Gt" in text
    for unit in division.units:
        assert unit in text


def test_the_plan_line_scales_with_the_plan() -> None:
    """Sama funktio, eri suunnitelma, eri luvut -- muuten luku on koriste."""
    small = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=1, estimated_bytes=1024**2
        ),
        None,
        "kohde",
    )
    large = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=tuple(f"a-{i}" for i in range(1001)),
            matches_played=501,
            estimated_bytes=1024**4,
        ),
        None,
        "kohde",
    )

    assert "1 demoa, arviolta 1,0 Mt" in small
    assert "1001 demoa, arviolta 1,0 Tt" in large
    # Vapaata tilaa ei tiedetä: riviä ei keksitä.
    assert "Levytilaa vapaana" not in small


def test_the_plan_says_the_index_age_it_was_given() -> None:
    """Ikä tulee suunnitelmalta: kiinteä teksti menisi läpi joka ajolla."""
    vanha = _render_collect_plan(
        fetch_stage.CollectPlan(index_generated_at="2026-09-04T18:20:11+00:00"),
        None,
        "kohde",
    )
    tuore = _render_collect_plan(
        fetch_stage.CollectPlan(index_generated_at="2026-09-06T17:05:00+00:00"),
        None,
        "kohde",
    )

    assert "2026-09-04T18:20:11+00:00" in vanha
    assert "2026-09-06T17:05:00+00:00" in tuore
    assert vanha != tuore


def test_an_index_without_a_timestamp_does_not_invent_one() -> None:
    text = _render_collect_plan(fetch_stage.CollectPlan(), None, "kohde")

    assert "aika tuntematon" in text


# -- Oletusmoodi ja paikallinen moodi kulkevat komennon läpi -----------------


def test_the_local_demos_root_setting_moves_the_files_out_of_the_archive(
    settings_file_local_demos, tmp_path, monkeypatch
) -> None:
    local = tmp_path / LOCAL_DEMOS_DIRNAME
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file_local_demos))
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    settings = load_settings()
    archive = archive_paths(settings.project)
    assert archive.demos_root == local

    source = FakeDemoSource({})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda _s, _a: source
    )
    world = Division(archive, settings, source)
    units = world.discover()

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    for unit in units:
        assert (local / f"{unit}.dem.zst").read_bytes() == DEMO_BYTES
        assert (local / f"{unit}.meta.json").is_file()
    assert not archive.archive_demos_dir().exists()
    assert str(local) in result.output


def test_without_demos_root_the_demos_go_into_the_archive(
    settings_file, monkeypatch
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    settings = load_settings()
    archive = archive_paths(settings.project)
    assert archive.demos_root is None

    source = FakeDemoSource({})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda _s, _a: source
    )
    world = Division(archive, settings, source)
    units = world.discover()

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    for unit in units:
        assert (archive.root / "demos" / f"{unit}.dem.zst").is_file()
        assert (archive.root / "demos" / f"{unit}.meta.json").is_file()


# -- Keräys ei kirjoita indeksiin eikä aja muita vaiheita --------------------


def test_collect_does_not_touch_the_match_index(division) -> None:
    """``collect`` on indeksin lukija. Kirjoitus olisi toinen ``discover``."""
    path = division.archive.matches_index()
    before = path.read_bytes()

    runner.invoke(app, ["collect", "--kylla"])

    assert path.read_bytes() == before


def test_collect_does_not_run_discover(division, monkeypatch) -> None:
    """Otteluindeksin virkistäminen on oma päätöksensä, ei sivutuote."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("collect ei saa kutsua discoveria")

    monkeypatch.setattr(discover_stage, "run", refuse)
    monkeypatch.setattr(discover_stage, "default_source", refuse)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output


def test_collect_writes_no_selection_file(division) -> None:
    """Rosterikynnys on ``select``in asia, eikä keräys tuota valintaa."""
    runner.invoke(app, ["collect", "--kylla"])

    selections = division.archive.resolve("index/selections")
    assert not selections.exists() or not list(selections.glob("*.json"))
