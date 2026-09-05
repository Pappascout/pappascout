"""``pappascout fetch`` -- komennon testit (Story 3.4).

Neljä asiaa lukitaan täällä:

* **Kysymys ennen latausta.** Komento kertoo montako demoa haetaan, minne ja
  paljonko tilaa ne vievät, ja odottaa vastausta. ``--kylla`` ohittaa
  kysymyksen -- muttei suunnitelman tulostamista.
* **Ei-vastaus ei lataa mitään.** Vahvistuksen peruminen on peruminen, ei
  viive.
* **Kerrossääntö.** Komento ei tuo adaptereita eikä arkistoa: portti tulee
  ``stages.fetch.default_source``ilta ja polut ``stages.archive_paths``ilta.
* **Muu kuin ``ok`` näkyy syineen.** Poistettu demo ja katkennut yhteys ovat
  eri jatko, eivätkä ne saa näyttää ruudulla samalta.

Koko ketju ``discover`` -> ``select`` -> ``fetch`` ajetaan oikeilla vaiheilla
feikkiporttien takaa: se on ainoa tapa todistaa, että valintatiedoston muoto ja
sen lukija pysyvät yhdessä.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_stage_discover import CHAMPIONSHIP, FakeSource, division_matches
from conftest import LOCAL_DEMOS_DIRNAME
from test_stage_fetch import DEMO_BYTES, FakeDemo, FakeDemoSource
from typer.testing import CliRunner

from pappascout.cli import (
    EXIT_KNOWN_ERROR,
    _render_fetch,
    _render_fetch_plan,
    _render_info,
    app,
    main,
)
from pappascout.domain.models import SETTINGS_ENV_VAR
from pappascout.stages import StageResult, archive_paths
from pappascout.stages import discover as discover_stage
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import select as select_stage

runner = CliRunner()

SUBJECT = "Potku"


@pytest.fixture(params=["arkisto", "paikallinen"])
def pipeline(request, settings_file: Path, tmp_path: Path, monkeypatch):
    """Oikeat vaiheet, feikatut portit, arkisto väliaikaishakemistossa.

    **Molemmat demohakemistomoodit, jokaisessa testissä.** Versioitu
    ``settings.toml`` valitsee arkiston, mutta ``[project].demos_root`` on
    tuettu moodi -- ja moodi, jota komentotestit eivät aja, kulkisi CLI:n läpi
    nolla kertaa. Aukon kääntäminen toisin päin ei ole korjaus.

    Palauttaa ``(archive, source, units)``.
    """
    if request.param == "paikallinen":
        text = settings_file.read_text(encoding="utf-8")
        line = next(
            r for r in text.splitlines() if r.startswith("# demos_root = ")
        )
        settings_file.write_text(
            text.replace(line, f"demos_root = '{tmp_path / 'paikalliset'}'", 1),
            encoding="utf-8",
        )
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )

    from pappascout.domain.models import load_settings

    settings = load_settings()
    archive = archive_paths(settings.project)
    discover_stage.run(
        settings.league,
        archive,
        None,
        source=FakeSource({CHAMPIONSHIP: division_matches()}),
        thresholds=settings.thresholds,
    )
    result = select_stage.run(
        settings.league, archive, SUBJECT, thresholds=settings.thresholds
    )
    team_key = result.unit

    document = select_stage.read_selection(archive, team_key)
    units = [
        row["map_demo_id"] for row in document["selections"] if row["roster_ok"]
    ]
    assert units, "aineisto ei tuottanut yhtäkään valittua karttaa"

    source = FakeDemoSource({unit: FakeDemo(DEMO_BYTES) for unit in units})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source",
        lambda settings, archive: source,
    )
    # Levy ei saa olla testin muuttuja: tarkistus on oma testinsä.
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)
    return archive, source, units


def test_the_plan_is_shown_and_confirmed_before_anything_is_downloaded(
    pipeline,
) -> None:
    archive, source, units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="k\n")

    assert result.exit_code == 0, result.output
    # **Koko luku, ei osajono.** ``str(len(units))`` osuisi yksinumeroisena
    # karttatunnisteeseen ja ajoaikaan, ja väite menisi läpi silloinkin kun
    # ruudulla lukee jokin aivan muu luku.
    assert f"{len(units)} demoa" in result.output
    assert "Ladataanko nämä demot?" in result.output
    assert source.asked == units
    for unit in units:
        assert archive.demo(unit).read_bytes() == DEMO_BYTES


def test_answering_no_downloads_nothing(pipeline) -> None:
    archive, source, units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="e\n")

    # Kieltävä vastaus ei ole virhe: käyttäjä sai kysymyksen ja vastasi siihen.
    assert result.exit_code == 0, result.output
    assert "Peruttu" in result.output
    assert source.asked == []
    assert archive.find_demo(units[0]) is None


def test_the_question_and_its_options_are_in_finnish(pipeline) -> None:
    """Kaikki käyttäjälle näkyvä on suomeksi -- myös vaihtoehdot ja peruminen.

    ``typer.confirm`` tulostaa ``[y/N]`` ja ``Aborted.``, eli käyttäjän pitäisi
    painaa ``y`` ja lukea englantia työkalussa, jonka jokainen muu rivi on
    suomeksi.
    """
    _archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="e\n")

    assert "[k/e]" in result.output
    assert "[y/N]" not in result.output
    assert "Aborted" not in result.output
    assert source.asked == []


def test_k_is_the_answer_that_downloads(pipeline) -> None:
    _archive, source, units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="k\n")

    assert result.exit_code == 0, result.output
    assert source.asked == units


def test_an_empty_answer_does_not_download(pipeline) -> None:
    """Enter ei ole kyllä: oletus on se, joka ei kuluta kiintiötä eikä levyä."""
    _archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="\n")

    assert result.exit_code == 0, result.output
    assert source.asked == []


def test_an_unrecognised_answer_does_not_download(pipeline) -> None:
    """Väärin ymmärretty vastaus ei saa johtaa 2,3 GB:n lataukseen."""
    _archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="ehkä\n")

    assert result.exit_code == 0, result.output
    assert source.asked == []


def test_kylla_skips_the_question_but_not_the_plan(pipeline) -> None:
    archive, source, units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--kylla"])

    assert result.exit_code == 0, result.output
    assert "Ladataanko" not in result.output
    assert f"{len(units)} demoa" in result.output
    assert source.asked == units


def test_the_plan_names_the_target_directory(pipeline) -> None:
    archive, _source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="n\n")

    assert str(archive.demos_dir()) in result.output


def test_a_second_run_downloads_nothing_and_says_so(pipeline) -> None:
    _archive, source, units = pipeline

    first = runner.invoke(app, ["fetch", "--team", SUBJECT, "--kylla"])
    assert first.exit_code == 0, first.output
    source.asked.clear()

    second = runner.invoke(app, ["fetch", "--team", SUBJECT, "--kylla"])

    assert second.exit_code == 0, second.output
    assert source.asked == []
    assert "ei ladattavaa" in second.output


def test_a_missing_demo_is_listed_with_its_reason(
    pipeline, monkeypatch
) -> None:
    """Poistettu demo ei ole virhe, mutta se on kerrottava."""
    from pappascout.errors import DemoUnavailable

    _archive, source, units = pipeline
    source.demos[units[0]] = DemoUnavailable(
        "FACEIT on poistanut tallenteen (säilytys noin 30 päivää)."
    )

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--kylla"])

    assert result.exit_code == 0, result.output
    assert "Ei saatavilla" in result.output
    assert "30 päivää" in result.output
    assert units[0] in result.output


def test_a_failed_download_is_listed_separately_from_a_missing_one(
    pipeline,
) -> None:
    from pappascout.errors import ApiError

    _archive, source, units = pipeline
    source.demos[units[0]] = ApiError("Rajapinta ei vastannut.", status_code=503)

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--kylla"])

    assert result.exit_code == 0, result.output
    assert "Epäonnistui" in result.output
    # Otsikko **toteaa vain mitä tapahtui**: neuvo tulee vian mukana (D1).
    assert "Epäonnistui (1) -- aja komento uudelleen" not in result.output


def test_a_full_disk_stops_the_command_before_the_question(
    pipeline, monkeypatch, capsys
) -> None:
    _archive, source, _units = pipeline
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 1024)
    monkeypatch.setattr("sys.argv", ["pappascout", "fetch", "--team", SUBJECT])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    assert "Levytila ei riitä" in captured.err + captured.out
    assert "Ladataanko" not in captured.out
    assert source.asked == []


def test_without_a_selection_file_the_error_says_to_run_select(
    settings_file, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )
    runner.invoke(app, ["discover"])
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "fetch", "--team", SUBJECT, "--kylla"]
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    assert "select" in captured.err + captured.out


def test_the_local_demos_root_setting_moves_the_files_out_of_the_archive(
    settings_file_local_demos, tmp_path, monkeypatch
) -> None:
    """``[project].demos_root`` ohjaa demot OneDriven ulkopuolelle.

    Rivi on versioidussa tiedostossa kommentoituna -- arkisto on oletus, koska
    se seuraa koneelta toiselle ja OneDrive vapauttaa parsitun demon tilan
    poistamatta tiedostoa. Moodi on silti tuettu, ja tämä testi ajaa koko
    komennon oikealla asetustiedostolla: asetus, ``ArchivePaths`` ja vaihe
    todistetaan yhdessä, koska yksikään niistä ei yksin osoita, että tiedosto
    päätyy toiseen hakemistoon.
    """
    from pappascout.domain.models import load_settings

    local = tmp_path / LOCAL_DEMOS_DIRNAME
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file_local_demos))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    settings = load_settings()
    archive = archive_paths(settings.project)
    assert archive.demos_root == local

    units = _prepare(archive, monkeypatch)

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--kylla"])

    assert result.exit_code == 0, result.output
    for unit in units:
        assert (local / f"{unit}.dem.zst").read_bytes() == DEMO_BYTES
        assert (local / f"{unit}.meta.json").is_file()
    assert not archive.archive_demos_dir().exists()
    assert str(local) in result.output


def _prepare(archive, monkeypatch) -> list[str]:
    """Aja discover ja select, ja johdota demolähde valituille kartoille."""
    runner.invoke(app, ["discover"])
    runner.invoke(app, ["select", "--team", SUBJECT])
    document = json.loads(
        next(archive.resolve("index/selections").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    units = [r["map_demo_id"] for r in document["selections"] if r["roster_ok"]]
    source = FakeDemoSource({unit: FakeDemo(DEMO_BYTES) for unit in units})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda s, a: source
    )
    return units


# -- Ruudun luvut (B3, 2026-09-05) -------------------------------------------
#
# Vahvistuskysymyksen **koko arvo on luvuissa**: "Ladataan 12 demoa, arviolta
# 2,6 Gt" on kysymys, johon voi vastata, ja "Ladataan 1001 demoa, arviolta
# 168,1 Gt" on eri kysymys. Jos luvut eivät ole vartioituja, ne voivat olla
# mitä tahansa eikä mikään huomaa -- ja käyttäjä vastaisi väärään kysymykseen.


@pytest.mark.parametrize(
    "num_bytes,expected",
    [
        (0, "0 tavua"),
        (1023, "1023 tavua"),
        (1024, "1,0 kt"),
        (1536, "1,5 kt"),
        (1024**2, "1,0 Mt"),
        (234_163_493, "223,3 Mt"),
        (fetch_stage.DEMO_SIZE_ESTIMATE_BYTES, "256,0 Mt"),
        (12 * 223 * 1024**2, "2,6 Gt"),
        (1024**4, "1,0 Tt"),
    ],
)
def test_sizes_are_formatted_with_a_finnish_decimal_comma(
    num_bytes: int, expected: str
) -> None:
    assert fetch_stage.size_fi(num_bytes) == expected


def test_the_plan_line_says_the_real_count_and_the_real_size() -> None:
    """Suunnitelman luvut tulevat suunnitelmasta, eivät mistään muualta."""
    todo = fetch_stage.FetchPlan(
        team_key="joukkue",
        pending=("a-0", "a-1", "b-0"),
        present=("c-0",),
        estimated_bytes=3 * 223 * 1024**2,
    )

    text = _render_fetch_plan(todo, 9_900_000_000, r"D:\demot")

    assert "Otanta: 4 karttaa, joista 1 on jo levyllä" in text
    assert "3 demoa, arviolta 669,0 Mt" in text
    assert r"D:\demot" in text
    assert "9,2 Gt" in text
    for unit in todo.pending:
        assert unit in text


def test_the_plan_line_scales_with_the_plan() -> None:
    """Sama funktio, eri suunnitelma, eri luvut -- muuten luku on koriste."""
    small = _render_fetch_plan(
        fetch_stage.FetchPlan("t", pending=("a-0",), estimated_bytes=1024**2),
        None,
        "kohde",
    )
    large = _render_fetch_plan(
        fetch_stage.FetchPlan(
            "t", pending=tuple(f"a-{i}" for i in range(1001)), estimated_bytes=1024**4
        ),
        None,
        "kohde",
    )

    assert "1 demoa, arviolta 1,0 Mt" in small
    assert "1001 demoa, arviolta 1,0 Tt" in large
    # Vapaata tilaa ei tiedetä: riviä ei keksitä.
    assert "Levytilaa vapaana" not in small


def fetch_result(unit: str, status: str, **stats) -> StageResult:
    """Vaiheen tulos tulosteen testaamiseen.

    ``next_step`` on oletuksena olemassa epäonnistuneilla, koska vaihe ei
    tuota sellaista riviä ilman sitä (``stages.fetch._result`` vartioi asian).
    """
    if status != "ok":
        stats.setdefault("next_step", "Tee jotain.")
    return StageResult(
        stage="fetch",
        unit=unit,
        status=status,
        skipped=stats.pop("skipped", False),
        reason=stats.pop("reason", None),
        duration_s=stats.pop("duration_s", 1.0),
        stats={"downloaded_bytes": 0, **stats},
    )


def test_the_summary_counts_every_status_separately() -> None:
    """Neljä lukua, neljä eri jatkoa -- eikä yksikään saa vuotaa toiseen."""
    todo = fetch_stage.FetchPlan("t", pending=("a-0",), present=("z-0", "z-1"))
    results = (
        fetch_result("a-0", "ok", downloaded_bytes=1024**2, demos_dir=r"D:\demot"),
        fetch_result("b-0", "ok", downloaded_bytes=2 * 1024**2, demos_dir=r"D:\demot"),
        fetch_result("c-0", "ok", skipped=True),
        fetch_result("d-0", "no_demo", reason="FACEIT poisti tallenteen."),
        fetch_result("e-0", "download_failed", reason="Yhteys katkesi."),
    )

    text = _render_fetch(results, todo)

    assert "2 haettu" in text
    # Ohitetut + suunnitelman jo levyllä olleet: 1 + 2.
    assert "3 oli jo levyllä" in text
    assert "1 ei saatavilla" in text
    assert "1 epäonnistui" in text
    assert "Kirjoitettu" in text and "3,0 Mt" in text
    assert r"D:\demot" in text


def test_the_summary_lists_every_reason_not_just_the_count() -> None:
    """Poistettu demo ja katkennut yhteys ovat eri jatko."""
    todo = fetch_stage.FetchPlan("t", pending=())
    results = (
        fetch_result("d-0", "no_demo", reason="FACEIT poisti tallenteen 30 pv."),
        fetch_result("e-0", "download_failed", reason="Yhteys katkesi."),
    )

    text = _render_fetch(results, todo)

    assert "Ei saatavilla (1)" in text
    assert "FACEIT poisti tallenteen 30 pv." in text
    assert "Epäonnistui (1)" in text
    assert "Yhteys katkesi." in text
    assert "d-0" in text and "e-0" in text


def test_the_summary_sums_the_real_byte_counts() -> None:
    todo = fetch_stage.FetchPlan("t", pending=())
    results = tuple(
        fetch_result(f"a-{i}", "ok", downloaded_bytes=100 * 1024**2)
        for i in range(12)
    )

    text = _render_fetch(results, todo)

    assert "12 haettu" in text
    assert "Kirjoitettu" in text and "1,2 Gt" in text


# -- Oletusmoodi kulkee komennon läpi (B6) -----------------------------------


def test_without_demos_root_the_demos_go_into_the_archive(
    settings_file, monkeypatch
) -> None:
    """Toimitettava oletus: demot arkiston ``demos/``iin.

    Päätetty 2026-09-05. Arkisto on OneDrivessa ja seuraa koneelta toiselle,
    joten projektin koko tila liikkuu yhtenä kokonaisuutena -- ja Files
    On-Demand vapauttaa parsitun demon paikallisen tilan **poistamatta
    tiedostoa**, mikä paikallisessa kansiossa olisi lopullinen poisto.
    """
    from pappascout.domain.models import load_settings

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    settings = load_settings()
    archive = archive_paths(settings.project)
    assert archive.demos_root is None

    units = _prepare(archive, monkeypatch)

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--kylla"])

    assert result.exit_code == 0, result.output
    for unit in units:
        assert (archive.root / "demos" / f"{unit}.dem.zst").is_file()
        assert (archive.root / "demos" / f"{unit}.meta.json").is_file()
    assert "(paikallinen)" not in result.output


# -- Levytilaportti osuu oikeasti vaiheeseen (B9) ----------------------------


def test_a_disk_that_fills_up_between_demos_stops_only_that_demo(
    pipeline, monkeypatch
) -> None:
    """Vaiheen oma portti, ei komennon: tila voi loppua kesken sarjan.

    Testi todistaa samalla, että vaiheen ``disk_free`` todella kysyy
    :func:`free_space`ilta ajohetkellä -- oletusarvoksi sidottu funktio ei
    reagoisi tähän monkeypatchiin lainkaan, ja testi menisi läpi vain siksi,
    että koneella on tilaa.
    """
    _archive, source, units = pipeline
    calls = {"n": 0}

    def shrinking(_archive):
        calls["n"] += 1
        return 100 * 1024**3 if calls["n"] <= 2 else 1024

    monkeypatch.setattr(fetch_stage, "free_space", shrinking)

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--kylla"])

    assert result.exit_code == 0, result.output
    assert "Levytila ei riitä" in result.output
    assert len(source.asked) < len(units)


# -- info-komennon Demot-rivi (B7) -------------------------------------------


def test_info_names_the_demo_directory(
    settings_file_local_demos, monkeypatch, tmp_path
) -> None:
    from pappascout.domain.models import load_settings

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file_local_demos))

    rendered = _render_info(load_settings())

    local = tmp_path / LOCAL_DEMOS_DIRNAME
    assert f"Demot              {local} (paikallinen)" in rendered


def test_info_says_the_archive_directory_when_there_is_no_local_one(
    settings_file, monkeypatch
) -> None:
    """``(paikallinen)`` on väite, ei koriste: se ei saa näkyä väärässä moodissa."""
    from pappascout.domain.models import load_settings

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    settings = load_settings()
    rendered = _render_info(settings)
    archive = archive_paths(settings.project)

    assert f"Demot              {archive.root / 'demos'}" in rendered
    assert "(paikallinen)" not in rendered


# -- Kohdehakemisto on osa kysymystä (Veeti 2026-09-05) ----------------------
#
# Veeti ehdotti, että työkalu kysyisi mihin tallennetaan. Erillistä kysymystä ei
# tehdä -- asetusrivi ja info-komento ovat jo se vastaus, ja joka ajolla
# toistuva kysymys olisi kohinaa. Sen sijaan **vahvistuskysymys kertoo
# kohteen** siinä missä se kertoo lukumäärän ja koon: käyttäjä näkee mihin
# ollaan kirjoittamassa juuri silloin kun se ratkeaa, ja voi keskeyttää jos se
# on väärä. Polku on siksi vartioitava samalla tarkkuudella kuin luvut.


def test_the_target_directory_is_shown_before_the_question(pipeline) -> None:
    archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="e\n")

    kohde = [r for r in result.output.splitlines() if r.strip().startswith("Kohde")]
    assert kohde, f"suunnitelmassa ei ole Kohde-riviä:\n{result.output}"
    assert str(archive.demos_dir()) in kohde[0]
    # Kysymys tulee vasta kohteen jälkeen: muuten sen näkisi vasta vastattuaan.
    assert result.output.index("Kohde") < result.output.index("Ladataanko")
    assert source.asked == []


def test_the_shown_target_is_the_directory_that_is_written_to(pipeline) -> None:
    """Polku ei saa olla koriste: sen on oltava sama, johon tiedostot menevät.

    Kiinteä tai väärä polku menisi läpi jokaisesta pelkkää olemassaoloa
    tarkistavasta väitteestä -- ja käyttäjä hyväksyisi latauksen väärään
    paikkaan luullen tarkistaneensa sen.
    """
    archive, _source, units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--kylla"])

    assert result.exit_code == 0, result.output
    written = archive.find_demo(units[0])
    assert written is not None
    kohde = [r for r in result.output.splitlines() if r.strip().startswith("Kohde")]
    assert kohde, f"tulosteessa ei ole Kohde-riviä:\n{result.output}"
    assert all(str(written.parent) in row for row in kohde)


def test_the_target_line_distinguishes_the_two_modes(tmp_path) -> None:
    """Sama rivi, eri moodi, eri polku -- muuten rivi ei kerro mitään.

    Kohde tulee ``demos_dir()``iltä, joten kiinteä polku menisi läpi jokaisesta
    pelkkää olemassaoloa tarkistavasta väitteestä.
    """
    from pappascout.archive.paths import ArchivePaths

    arkisto = ArchivePaths(root=tmp_path / "arkisto")
    paikallinen = ArchivePaths(
        root=tmp_path / "arkisto", demos_root=tmp_path / LOCAL_DEMOS_DIRNAME
    )

    todo = fetch_stage.FetchPlan("t", pending=("a-0",), estimated_bytes=1024**2)
    arkistorivi = _render_fetch_plan(todo, None, str(arkisto.demos_dir()))
    paikallisrivi = _render_fetch_plan(todo, None, str(paikallinen.demos_dir()))

    assert str(tmp_path / "arkisto" / "demos") in arkistorivi
    assert str(tmp_path / LOCAL_DEMOS_DIRNAME) in paikallisrivi
    assert arkistorivi != paikallisrivi


# -- Ensimmäinen oikea ajo verkkoa vasten (2026-09-05) ----------------------


@pytest.fixture
def denied_pipeline(pipeline, monkeypatch):
    """Sama ketju, mutta oikea adapteri vastaa 403:lla signauskutsuun.

    **Oikea adapteri eikä feikkiporttti**, koska juuri se sauma erosi: feikki
    ei voi tuottaa Downloads API:n 403:a, ja sen takia kaksi katselmusta ja
    koko testisarja menivät ohi viasta, jonka ensimmäinen oikea ajo löysi
    seitsemässä sekunnissa.
    """
    from test_faceit_demos import DOWNLOADS, FakeResponse, FakeSession, build

    archive, _source, units = pipeline
    session = FakeSession(sign=FakeResponse(403), download=FakeResponse(200))
    real_source = build(archive.root.parent, session)
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda s, a: real_source
    )
    assert real_source.downloads_base_url == DOWNLOADS
    return archive, session, units


def test_a_denied_downloads_token_does_not_tell_the_user_to_retry(
    denied_pipeline, monkeypatch, capsys
) -> None:
    """**C1.** "Aja komento uudelleen" ei auta ennen kuin hakemus hyväksytään.

    Mitattu 2026-09-05: Veetin hakemus oli jonossa ("waiting for review"), ja
    työkalu lajitteli 403:n otsikon "Epäonnistui (2) -- aja komento uudelleen"
    alle. Neuvo oli väärä, ja se olisi toistunut jokaisella ajolla.
    """
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "fetch", "--team", SUBJECT, "--kylla"]
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    out = capsys.readouterr()
    text = out.out + out.err
    assert "aja komento uudelleen" not in text.lower()
    assert "Epäonnistui" not in text
    assert "fc-downloads.loza.gg" in text
    assert "downloads-api-application" in text
    assert "FACEIT_DOWNLOADS_TOKEN" in text


def test_only_one_signing_call_is_made_before_the_run_stops(
    denied_pipeline, monkeypatch, capsys
) -> None:
    """**C2.** Kahdellatoista demolla tämä olisi ollut 12 tuomittua kutsua."""
    _archive, session, units = denied_pipeline
    assert len(units) > 1, "aineisto ei todista mitään yhdellä yksiköllä"
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "fetch", "--team", SUBJECT, "--kylla"]
    )

    with pytest.raises(SystemExit):
        main()

    capsys.readouterr()
    assert len(session.posts) == 1


def test_the_denied_message_reaches_the_screen_in_finnish(
    denied_pipeline, monkeypatch, capsys
) -> None:
    _archive, _session, _units = denied_pipeline
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "fetch", "--team", SUBJECT, "--kylla"]
    )

    with pytest.raises(SystemExit):
        main()

    text = "".join(capsys.readouterr())
    assert "Downloads API on erillinen käyttöoikeus" in text
    assert "Odottaminen" in text


# -- Vastaamatta jättäminen on vastaus (C3) ---------------------------------


def test_no_input_at_all_is_cancelled_in_finnish(pipeline) -> None:
    """**C3.** ``typer`` keskeyttää EOF:iin omalla viestillään ``Aborted.``

    Se tapahtuu **ennen** kuin vahvistuksen oma koodi näkee mitään, joten A7:n
    korjaus ei kattanut tätä reittiä: sama englanninkielinen sana tuli eri
    kautta. Putki, ajastin ja Ctrl-C osuvat kaikki tähän.
    """
    _archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="")

    assert result.exit_code == 0, result.output
    assert "Aborted" not in result.output
    assert "Peruttu. Yhtään demoa ei ladattu." in result.output
    assert source.asked == []


def test_the_cancel_message_is_the_same_however_the_user_declines(
    pipeline,
) -> None:
    """Kieltävä vastaus ja vastaamatta jättäminen ovat sama lopputulos.

    Kaksi eri sanamuotoa antaisi ymmärtää että ne eroavat.
    """
    _archive, _source, _units = pipeline

    kielto = runner.invoke(app, ["fetch", "--team", SUBJECT], input="e\n")
    tyhja = runner.invoke(app, ["fetch", "--team", SUBJECT], input="")

    assert "Peruttu. Yhtään demoa ei ladattu." in kielto.output
    assert "Peruttu. Yhtään demoa ei ladattu." in tyhja.output
    assert kielto.exit_code == tyhja.exit_code == 0


# -- D1: neuvo kuuluu vikaan, ei otsikkoon (live-ajo 2026-09-05) ------------
#
# Kaksi peräkkäistä oikeaa ajoa löysi saman kuvion: luokittelu oli oikein mutta
# neuvo väärä, koska neuvo tuli otsikosta "Epäonnistui (N) -- aja komento
# uudelleen". Se on ämpäri, johon päätyy sekä ohimenevä häiriö että pysyvä
# vika, ja jokainen uusi vikaluokka peri väärän neuvon oletuksena.


def test_two_failures_from_different_causes_get_different_advice() -> None:
    """**Koko korjauksen pointti.**

    Sama tuloste, kaksi epäonnistunutta yksikköä, kaksi eri syytä -- ja kaksi
    eri neuvoa. Yhteinen otsikko voisi olla oikea enintään toiselle niistä, ja
    juuri se teki 403:sta ja 400:sta "aja komento uudelleen" -tapauksia.
    """
    todo = fetch_stage.FetchPlan("t", pending=("a-0", "b-0"))
    results = (
        fetch_result(
            "a-0",
            "download_failed",
            reason="Yhteys katkesi kesken latauksen.",
            next_step="Aja komento uudelleen.",
        ),
        fetch_result(
            "b-0",
            "download_failed",
            reason="FACEIT ei hyväksynyt latauslinkkipyyntöä (HTTP 400).",
            next_step="Tarkista FACEIT_DOWNLOADS_TOKEN koneesi .env-tiedostosta.",
        ),
    )

    text = _render_fetch(results, todo)

    assert "-> Aja komento uudelleen." in text
    assert "-> Tarkista FACEIT_DOWNLOADS_TOKEN" in text
    # Ja ne ovat eri riveillä eri yksiköiden alla, eivät yhteisessä otsikossa.
    rows = text.splitlines()
    a_index = next(i for i, r in enumerate(rows) if r.strip() == "a-0")
    b_index = next(i for i, r in enumerate(rows) if r.strip() == "b-0")
    assert "Aja komento uudelleen." in rows[a_index + 2]
    assert "FACEIT_DOWNLOADS_TOKEN" in rows[b_index + 2]


def test_the_failure_heading_states_what_happened_and_nothing_more() -> None:
    """Otsikko ei saa neuvoa, koska se ei tiedä mistä viasta on kyse."""
    todo = fetch_stage.FetchPlan("t", pending=("a-0",))
    results = (
        fetch_result("a-0", "download_failed", reason="x", next_step="y"),
    )

    text = _render_fetch(results, todo)

    heading = next(r for r in text.splitlines() if r.startswith("Epäonnistui"))
    assert heading == "Epäonnistui (1):"


def test_a_missing_demo_also_carries_its_own_advice() -> None:
    """Sama sääntö molemmissa ämpäreissä, ei vain toisessa."""
    todo = fetch_stage.FetchPlan("t", pending=("a-0",))
    results = (
        fetch_result(
            "a-0",
            "no_demo",
            reason="FACEIT on poistanut tallenteen.",
            next_step="Tuo demo käsin, jos se on tallessa.",
        ),
    )

    text = _render_fetch(results, todo)

    assert "Ei saatavilla (1):" in text
    assert "-> Tuo demo käsin, jos se on tallessa." in text


def test_a_multi_line_reason_stays_readable_under_its_unit() -> None:
    """Viestit ovat monirivisiä; sisennyksen on kannettava jokainen rivi."""
    todo = fetch_stage.FetchPlan("t", pending=("a-0",))
    results = (
        fetch_result(
            "a-0",
            "download_failed",
            reason="Eka rivi.\nToka rivi.\nKolmas rivi.",
            next_step="Tee jotain.",
        ),
    )

    text = _render_fetch(results, todo)

    for row in ("Eka rivi.", "Toka rivi.", "Kolmas rivi."):
        assert f"    {row}" in text
