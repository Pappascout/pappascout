"""``pappascout import`` -- komennon testit (Story 3.6).

Neljä asiaa lukitaan täällä:

* **``--kylla`` EI ohita karttatarkistuksen kysymystä.** Se on epicin oma
  vaatimus ja tämän työkalun ainoa kohta, jossa lippu ei hiljennä kysymystä.
  Sama lippu ohittaa ylikirjoituskysymyksen, ja juuri se ero on se, mitä
  testien on erotettava toisistaan.
* **Kysymys ennen siirtoa, ja suunnitelma ennen kysymystä.** Järjestys on
  vartioitu erikseen: portti voi olla kunnossa vaikka käyttäjä näkisi
  suunnitelman vasta jälkikäteen, ja silloin hän vastaa kysymykseen jonka
  perusteita hän ei ole nähnyt.
* **Ei-vastaus ei siirrä mitään.**
* **Jokainen tulosteen rivi on väite, ja väitteet tarkistetaan yksitellen.**
  Katselmus mutatoi ruudun arvoja ja sai kuusi valhetta yhtaikaa läpi 70
  testistä -- muun muassa tuodun demon lähteeksi ``downloads_api``, eli juuri
  sen väitteen, jonka koko tarina on olemassa kumoamaan. Siksi tuloste
  puretaan riveiksi ja jokainen arvo verrataan **levyltä laskettuun
  totuuteen**, ei toiseen tulosteen riviin.

Portit ovat feikkejä eikä verkkoa kosketa kertaakaan.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import LOCAL_DEMOS_DIRNAME
from test_stage_import import (
    FACEIT_NAME,
    FACEIT_NAME_PLAIN,
    MATCH,
    PLAIN_BYTES,
    UNIT,
    ZSTD_BYTES,
    FakeMapNameParser,
    FakeMatchSource,
    match,
)
from typer.testing import CliRunner

from pappascout.cli import EXIT_KNOWN_ERROR, app, main
from pappascout.domain.models import SETTINGS_ENV_VAR, load_settings
from pappascout.errors import PappascoutError
from pappascout.stages import archive_paths
from pappascout.stages import import_demo as import_stage

runner = CliRunner()


@pytest.fixture(params=["arkisto", "paikallinen"])
def tuonti(request, settings_file: Path, tmp_path: Path, monkeypatch):
    """Oikea komento, feikatut portit, arkisto väliaikaishakemistossa.

    **Molemmat demohakemistomoodit, jokaisessa testissä.** Sama peruste kuin
    ``fetch``in vastaavalla kiinnikkeellä: tuettu moodi, jota komentotestit
    eivät aja, kulkisi CLI:n läpi nolla kertaa.

    Palauttaa ``(archive, parser)`` -- feikkilukijan kartan nimen voi vaihtaa
    testin sisällä ennen komennon ajoa.
    """
    if request.param == "paikallinen":
        text = settings_file.read_text(encoding="utf-8")
        line = next(r for r in text.splitlines() if r.startswith("# demos_root = "))
        settings_file.write_text(
            text.replace(line, f"demos_root = '{tmp_path / LOCAL_DEMOS_DIRNAME}'", 1),
            encoding="utf-8",
        )
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    archive = archive_paths(load_settings().project)
    archive.import_dir().mkdir(parents=True, exist_ok=True)
    (archive.import_dir() / FACEIT_NAME).write_bytes(ZSTD_BYTES)

    parser = FakeMapNameParser({FACEIT_NAME: "de_ancient"})
    monkeypatch.setattr(
        import_stage,
        "default_source",
        lambda settings, arc: FakeMatchSource({MATCH: match()}),
    )
    monkeypatch.setattr(import_stage, "default_parser", lambda: parser)
    # Levy ei saa olla testin muuttuja: tarkistus on oma testinsä.
    monkeypatch.setattr(import_stage, "_free_space", lambda _path: 100 * 1024**3)
    return archive, parser


def invoke(*args: str, input: str | None = None, map_no: str = "1"):
    return runner.invoke(
        app, ["import", "--match", MATCH, "--map", map_no, *args], input=input
    )


def imported(archive) -> Path | None:
    return archive.find_demo(UNIT)


def rivit(output: str) -> dict[str, str]:
    """Pura tuloste sanakirjaksi ``otsikko -> arvo``.

    **Väite arvosta eikä osajonosta.** ``assert polku in output`` menisi läpi
    myös silloin, kun sama polku sattuu ruudulla toisella rivillä -- ja juuri
    se päällekkäisyys antoi katselmuksen mutaatioiden mennä läpi: demon
    kohdepolku tulostuu kahdesti, joten kumpi tahansa yksin sai valehdella.
    Kun rivi puretaan otsikoksi ja arvoksi, jokainen rivi vastaa omasta
    sisällöstään.
    """
    tulos: dict[str, str] = {}
    for rivi in output.splitlines():
        if not rivi.startswith("  ") or rivi.startswith("    "):
            continue
        runko = rivi[2:]
        # ``_line`` täyttää otsikon kiinteään leveyteen; kahden välilyönnin
        # jono erottaa otsikon arvosta.
        if "  " not in runko:
            continue
        otsikko, _, arvo = runko.partition("  ")
        tulos[otsikko.strip()] = arvo.strip()
    return tulos


# -- Onnistunut tuonti -------------------------------------------------------


def test_a_matching_map_needs_no_question_at_all(tuonti) -> None:
    """Täsmäävä kartta on täysi vastaus: ei kysyttävää.

    Kysymys, joka esitetään myös silloin kun mitään ei ole päätettävänä,
    opettaa vastaamaan siihen katsomatta -- ja silloin se ei enää suojaa
    siltä tapaukselta, jota varten se on olemassa.
    """
    archive, _parser = tuonti

    result = invoke()

    assert result.exit_code == 0, result.output
    assert "Tuodaanko" not in result.output
    assert imported(archive) is not None
    meta = json.loads(
        (archive.demos_dir() / f"{UNIT}.meta.json").read_text(encoding="utf-8")
    )
    assert meta["source"] == "import"


# -- C2: jokainen tulosteen rivi on väite ------------------------------------


def test_the_plan_names_the_unit_that_is_imported(tuonti) -> None:
    _archive, _parser = tuonti

    assert rivit(invoke().output)["Tuodaan"] == UNIT


def test_the_plan_names_the_source_file_that_is_read(tuonti) -> None:
    """Lähdepolku on oma väitteensä eikä kohdepolun kaiku."""
    archive, _parser = tuonti
    lahde = archive.import_dir() / FACEIT_NAME

    assert rivit(invoke().output)["Lähde"] == str(lahde)


def test_the_plan_names_the_target_path_that_is_written(tuonti) -> None:
    """Kohdepolku ruudulla on se polku, johon tiedosto todella syntyy."""
    archive, _parser = tuonti

    naytetty = rivit(invoke().output)["Kohde"]

    assert naytetty == str(archive.demos_dir() / f"{UNIT}.dem.zst")
    assert Path(naytetty).is_file()


def test_the_plan_size_is_the_size_of_the_file_on_disk(tuonti) -> None:
    """Koko luetaan levyltä, ei arvata.

    Katselmus lisäsi kokoon ``+999 Gt`` kahdessa paikassa ja 70 testiä meni
    läpi. Luku, jota mikään ei vertaa mihinkään, on koristetta.
    """
    from pappascout.cli import _human_size

    _archive, _parser = tuonti

    assert rivit(invoke().output)["Koko"] == _human_size(len(ZSTD_BYTES))


def test_the_plan_says_move_when_the_file_is_moved(tuonti) -> None:
    _archive, _parser = tuonti

    assert rivit(invoke().output)["Tapa"] == "siirto"


def test_the_plan_says_copy_when_the_file_is_copied(tuonti, tmp_path) -> None:
    """Kopio ja siirto ovat eri asia, ja ruudun on erotettava ne.

    Katselmus vaihtoi tekstin "kopio (lähde jää paikalleen)" siirron kohdalle
    ja 70 testiä meni läpi -- eli käyttäjä olisi voinut lukea ruudulta, että
    hänen tiedostonsa jää paikalleen, ja se olisi juuri poistettu.
    """
    _archive, parser = tuonti
    ulkoa = tmp_path / "ulkoa" / "oma.dem.zst"
    ulkoa.parent.mkdir(parents=True, exist_ok=True)
    ulkoa.write_bytes(ZSTD_BYTES)
    parser.names["oma.dem.zst"] = "de_ancient"

    result = invoke("--file", str(ulkoa))

    assert rivit(result.output)["Tapa"] == "kopio (lähde jää paikalleen)"
    assert ulkoa.is_file()


def test_the_plan_shows_both_map_observations(tuonti) -> None:
    """Molemmat havainnot näkyvät myös silloin, kun ne täsmäävät."""
    _archive, _parser = tuonti

    rows = rivit(invoke().output)

    assert rows["Kartta otsikosta"] == "de_ancient"
    assert rows["Kartta vetotiedosta"] == "de_ancient"
    assert rows["Karttatarkistus"] == "täsmää"


def test_the_screen_never_claims_a_match_when_there_is_none(tuonti) -> None:
    """**"Karttatarkistus täsmää" aidon poikkeaman kohdalla on pahin valhe.**

    Katselmus muutti ehdon aina todeksi ja sai ruudun sanomaan "täsmää"
    samalla ruudulla, jolla pakotettu kysymys esitetään -- eli käyttäjä
    lukisi kysymyksen ja sen yläpuolelta vakuutuksen siitä ettei mitään ole
    vialla.
    """
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="e\n")

    rows = rivit(result.output)
    assert rows["Karttatarkistus"].startswith("EI TÄSMÄÄ")
    assert rows["Kartta otsikosta"] == "de_nuke"
    assert rows["Kartta vetotiedosta"] == "de_ancient"


def test_the_plan_says_whether_completeness_could_be_checked(tuonti) -> None:
    """Pakattu tiedosto: ehjyys on tarkistettu kehyksen kokoa vasten."""
    _archive, _parser = tuonti

    assert rivit(invoke().output)["Ehjyys"].startswith("tarkistettu")


def test_the_plan_says_out_loud_when_completeness_cannot_be_checked(
    tuonti,
) -> None:
    """**Pakkaamattomalla demolla epävarmuus on kerrottava.**

    Mitattu 2026-09-05: puoliväliin katkaistu tiedosto näyttää joka suhteessa
    ehjältä -- oikea pääte, oikea kartan nimi. Pakatulla on kehyksen koko
    vastassa; pakkaamattomalla ei ole mitään, ja vaikeneminen näyttäisi
    varmuudelta.
    """
    archive, parser = tuonti
    (archive.import_dir() / FACEIT_NAME).unlink()
    (archive.import_dir() / FACEIT_NAME_PLAIN).write_bytes(PLAIN_BYTES)
    parser.names[FACEIT_NAME_PLAIN] = "de_ancient"

    result = invoke()

    assert rivit(result.output)["Ehjyys"].startswith("EI VOITU TARKISTAA")


def test_the_result_names_the_files_that_were_written(tuonti) -> None:
    """Tuloksen polut osoittavat tiedostoihin, jotka ovat olemassa."""
    archive, _parser = tuonti

    rows = rivit(invoke().output)

    assert rows["Demo"] == str(archive.demos_dir() / f"{UNIT}.dem.zst")
    assert rows["Metatiedot"] == str(archive.demos_dir() / f"{UNIT}.meta.json")
    assert Path(rows["Demo"]).is_file()
    assert Path(rows["Metatiedot"]).is_file()


def test_the_result_sha256_is_the_digest_of_the_written_file(tuonti) -> None:
    """Tiiviste ruudulla on **sen tiedoston** tiiviste, joka syntyi.

    Katselmus vaihtoi arvoksi ``"0"*64`` ja 70 testiä meni läpi. Tiiviste on
    se luku, jolla tuotu demo tunnistetaan myöhemmin -- ``parse`` lukee sen
    metatiedostosta eikä laske uudelleen, joten tämä on ainoa kerta jolloin
    se näkyy.
    """
    archive, _parser = tuonti

    rows = rivit(invoke().output)

    todellinen = hashlib.sha256(
        (archive.demos_dir() / f"{UNIT}.dem.zst").read_bytes()
    ).hexdigest()
    assert rows["sha256"] == todellinen
    meta = json.loads(Path(rows["Metatiedot"]).read_text(encoding="utf-8"))
    assert meta["sha256"] == todellinen


def test_the_result_says_the_demo_came_from_an_import(tuonti) -> None:
    """**Katselmus sai ruudun väittämään tuotua demoa ladatuksi.**

    ``downloads_api`` tuodun demon lähteenä on juuri se väite, jonka koko
    tarina on olemassa kumoamaan -- ja se meni läpi 70 testistä.
    """
    archive, _parser = tuonti

    rows = rivit(invoke().output)

    assert rows["Lähdemerkintä"] == "import"
    meta = json.loads(
        (archive.demos_dir() / f"{UNIT}.meta.json").read_text(encoding="utf-8")
    )
    assert rows["Lähdemerkintä"] == meta["source"]


def test_the_result_size_is_the_size_of_the_written_file(tuonti) -> None:
    from pappascout.cli import _human_size

    archive, _parser = tuonti

    rows = rivit(invoke().output)
    koko = (archive.demos_dir() / f"{UNIT}.dem.zst").stat().st_size

    assert rows["Koko"] == _human_size(koko)


def test_every_note_reaches_the_screen(tuonti) -> None:
    """**Huomiot eivät saa kadota.**

    Katselmus tyhjensi huomiosilmukan ja 70 testiä meni läpi -- eli kaikki
    varoitukset (korvattu tiedosto, orpo metatiedosto, tarkistamaton ehjyys,
    vahvistettu karttapoikkeama) olisivat voineet kadota ruudulta yhdellä
    rivillä.
    """
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke("--kylla", input="k\n")

    assert result.exit_code == 0, result.output
    # Poikkeama ja lähdetiedoston kohtalo ovat molemmat omia huomioitaan.
    assert "de_nuke" in result.output
    assert "poistettiin tuontikansiosta" in result.output


def test_the_run_time_is_reported(tuonti) -> None:
    _archive, _parser = tuonti

    assert "Ajoaika" in rivit(invoke().output)


# -- C3: suunnitelma ennen siirtoa -------------------------------------------


def test_the_plan_is_printed_before_anything_is_transferred(
    tuonti, monkeypatch
) -> None:
    """**Käyttäjän on nähtävä suunnitelma ennen kuin mitään tapahtuu.**

    Portti on kunnossa ilman tätäkin testiä -- ``run`` kutsutaan vasta
    kysymysten jälkeen -- mutta *näkeminen* ei ollut vartioitu: katselmus
    siirsi suunnitelman tulostuksen tuloksen alapuolelle ja 28 testiä meni
    läpi. Silloin käyttäjä vastaisi kysymykseen, jonka perusteita hän ei ole
    nähnyt.

    Väite tehdään pysäyttämällä siirto: jos suunnitelma tulostetaan vasta
    ``run``in jälkeen, se ei tulostu lainkaan.
    """
    _archive, _parser = tuonti

    def boom(*args, **kwargs):
        raise PappascoutError("siirto pysäytettiin", advice="tämä on testi")

    monkeypatch.setattr(import_stage, "run", boom)

    result = invoke()

    assert result.exit_code != 0
    rows = rivit(result.output)
    assert rows["Tuodaan"] == UNIT
    assert "Kohde" in rows
    assert "Karttatarkistus" in rows


def test_the_plan_is_printed_before_the_question_is_asked(tuonti) -> None:
    """Kysymys ilman perusteita on muodollisuus, ei kysymys."""
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    output = invoke(input="e\n").output

    assert output.index("Kartta otsikosta") < output.index("Tuodaanko")


# -- Karttapoikkeama: --kylla EI ohita ---------------------------------------


def test_kylla_does_not_skip_the_map_confirmation(tuonti) -> None:
    """**Epicin oma vaatimus.** Lippu ei hiljennä tätä kysymystä."""
    archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke("--kylla", input="e\n")

    assert result.exit_code == 0, result.output
    assert "Kartta ei täsmää" in result.output
    assert "Tuodaanko" in result.output
    assert imported(archive) is None
    assert (archive.import_dir() / FACEIT_NAME).read_bytes() == ZSTD_BYTES


def test_kylla_does_not_skip_the_question_when_there_is_no_veto_data(
    tuonti, monkeypatch
) -> None:
    """Tarkistamattomuus ei ole täsmäys, eikä lippu saa tehdä siitä sellaista."""
    archive, _parser = tuonti
    monkeypatch.setattr(
        import_stage,
        "default_source",
        lambda settings, arc: FakeMatchSource({MATCH: match(picks=())}),
    )

    result = invoke("--kylla", input="e\n")

    assert result.exit_code == 0, result.output
    assert "vetotieto" in result.output
    assert imported(archive) is None


def test_the_mismatch_question_offers_the_number_that_would_be_right(
    tuonti,
) -> None:
    """Kysymys tarjoaa tien eteenpäin eikä pelkkää epäilystä."""
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="e\n")

    assert "--map 2" in result.output


def test_answering_yes_to_the_mismatch_imports_anyway(tuonti) -> None:
    """Kysymys on kysymys eikä este: käyttäjä saa tietää ja päättää."""
    archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke("--kylla", input="k\n")

    assert result.exit_code == 0, result.output
    assert imported(archive) is not None


# -- Ylikirjoitus: --kylla ohittaa -------------------------------------------


def test_kylla_does_skip_the_overwrite_question(tuonti) -> None:
    """Sama lippu, eri kysymys, eri lopputulos -- ja se on tarkoitus."""
    archive, _parser = tuonti
    vanha = archive.demos_dir() / f"{UNIT}.dem.zst"
    vanha.parent.mkdir(parents=True, exist_ok=True)
    vanha.write_bytes(b"vanha")

    result = invoke("--kylla")

    assert result.exit_code == 0, result.output
    assert "Korvataanko" not in result.output
    assert vanha.read_bytes() == ZSTD_BYTES


def test_without_kylla_the_existing_file_is_not_overwritten_silently(
    tuonti,
) -> None:
    archive, _parser = tuonti
    vanha = archive.demos_dir() / f"{UNIT}.dem.zst"
    vanha.parent.mkdir(parents=True, exist_ok=True)
    vanha.write_bytes(b"vanha")

    result = invoke(input="e\n")

    assert result.exit_code == 0, result.output
    assert "Korvataanko" in result.output
    assert vanha.read_bytes() == b"vanha"


# -- Kieli -------------------------------------------------------------------


def test_the_question_and_the_cancellation_are_in_finnish(tuonti) -> None:
    """``typer.confirm``in ``[y/N]`` ja ``Aborted.`` eivät kuulu tänne."""
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="e\n")

    assert "[k/e]" in result.output
    assert "[y/N]" not in result.output
    assert "Aborted" not in result.output
    assert "Peruttu" in result.output


def test_the_cancellation_message_says_what_was_not_done(tuonti) -> None:
    """Peruminen kertoo mitä jäi tekemättä -- eikä puhu latauksesta."""
    archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="e\n")

    assert "Demoa ei tuotu" in result.output
    assert "ladattu" not in result.output
    assert (archive.import_dir() / FACEIT_NAME).is_file()


def test_an_empty_answer_does_not_import(tuonti) -> None:
    """Enter ei ole kyllä: oletus on se, joka ei muuta arkistoa."""
    archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="\n")

    assert result.exit_code == 0, result.output
    assert imported(archive) is None


def test_a_non_numeric_map_is_a_finnish_error(tuonti, monkeypatch, capsys) -> None:
    """**``--map abc`` ei saa kaatua typerin englanninkieliseen viestiin.**

    Aiemmin komentorivi julisti arvon kokonaisluvuksi, jolloin ``typer``
    kaatui ennen kuin vaihe näki mitään -- ja vaiheen oma suomenkielinen
    tarkistus oli kuollutta koodia, saavuttamattomissa komentoriviltä.
    """
    _archive, _parser = tuonti
    monkeypatch.setattr(
        "sys.argv",
        ["pappascout", "import", "--match", MATCH, "--map", "abc"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    output = captured.err + captured.out
    assert "ei ole kokonaisluku" in output
    assert "Invalid value" not in output


# -- Virheet ruudulla --------------------------------------------------------


def test_a_rejection_shows_its_advice_on_its_own_line(
    tuonti, monkeypatch, capsys
) -> None:
    """Neuvo kulkee virheen mukana ja tulostuu omalle rivilleen."""
    _archive, _parser = tuonti
    monkeypatch.setattr(
        "sys.argv",
        ["pappascout", "import", "--match", MATCH, "--map", "0"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    output = captured.err + captured.out
    assert "alkaa ykk" in output
    assert "-> " in output


def test_two_candidates_are_listed_on_screen(tuonti, monkeypatch, capsys) -> None:
    """Monitulkintaisuutta ei ratkaista hiljaa -- ei myöskään ruudulla."""
    archive, parser = tuonti
    (archive.import_dir() / FACEIT_NAME_PLAIN).write_bytes(PLAIN_BYTES)
    parser.names[FACEIT_NAME_PLAIN] = "de_ancient"
    monkeypatch.setattr(
        "sys.argv",
        ["pappascout", "import", "--match", MATCH, "--map", "1", "--kylla"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    output = captured.err + captured.out
    assert FACEIT_NAME in output
    assert FACEIT_NAME_PLAIN in output
    # Neuvo ohjaa pakattuun ja on lainausmerkeissä, jotta sen voi kopioida.
    assert f'--file "{archive.import_dir() / FACEIT_NAME}"' in output
    assert imported(archive) is None


# -- Ohje --------------------------------------------------------------------


def test_help_mentions_that_kylla_does_not_skip_the_map_check(tuonti) -> None:
    """Poikkeussääntö on ohjeessa: muuten sen löytää vain törmäämällä siihen."""
    result = runner.invoke(app, ["import", "--help"])

    assert result.exit_code == 0
    assert "EI ohita" in result.output
