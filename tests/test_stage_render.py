"""``stages.render`` -- vaiheen testit.

Vaihe lukee yhden tiedoston ja kirjoittaa yhden tiedoston, joten sen koko
logiikka -- joukkueen valinta, syötteen tarkistus, aikaleimattu nimi, atominen
kirjoitus ja manifesti -- testataan väliaikaisessa arkistossa ilman demoja.

Raportin **sisältö** testataan ``test_render``issä ja komennon tuloste
``test_cli_report``issa; täällä testataan vain se, mitä vaihe tekee
tiedostoille.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from conftest import has_temp_leftovers
from pappascout.archive.manifest import Manifest, ManifestInput
from pappascout.archive.paths import MAX_REPORTS_PER_MINUTE, ArchivePaths, report_name
from pappascout.domain.report import Report
from pappascout.errors import PappascoutError
from pappascout.stages import render as render_stage
from test_render import DEMO_ID, TEAM_KEY, pistol_map, report

OTHER_TEAM = "bbbbbbbbbbbbbbbb"
STAMP = datetime(2026, 8, 30, 3, 7)


# --- Arkiston rakennus ----------------------------------------------------------


def build_archive(
    tmp_path: Path,
    *,
    teams: dict[str, Report] | None = None,
    write_manifest: bool = True,
) -> ArchivePaths:
    """Arkisto, jossa on annettujen joukkueiden ``report.json``-tiedostot."""
    archive = ArchivePaths(root=tmp_path / "arkisto")
    entries = teams if teams is not None else {TEAM_KEY: report([pistol_map()])}
    for team_key, entry in entries.items():
        path = archive.report_json(team_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry.model_dump_json(indent=2), encoding="utf-8")
        if write_manifest:
            Manifest.new(
                result_id=f"aggregates/{team_key}",
                stage="aggregate",
                params_hash="hash",
                inputs=[ManifestInput(result_id=f"classified/{team_key}", sha256="x")],
                outputs=(f"aggregates/{team_key}/report.json",),
            ).write(archive.report_manifest(team_key))
    return archive


def run(archive: ArchivePaths, team: str | None = TEAM_KEY, **kwargs):
    return render_stage.run(archive, team, **kwargs)


def reports(archive: ArchivePaths, team_key: str = TEAM_KEY) -> list[Path]:
    return sorted(archive.reports_dir(team_key).glob("*.md"))


# --- Perusajo -------------------------------------------------------------------


def test_run_writes_one_timestamped_markdown_file(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    result = run(archive)

    assert result.stage == "render"
    assert result.status == "ok"
    assert not result.skipped
    assert len(result.outputs) == 1

    written = archive.resolve(result.outputs[0])
    assert written.is_file()
    assert written.suffix == ".md"
    assert written.parent == archive.reports_dir(TEAM_KEY)
    assert written.read_text(encoding="utf-8").startswith("# MatureMayhem")


def test_the_file_on_disk_is_exactly_what_render_produced(tmp_path: Path) -> None:
    """Vaihe ei muotoile mitään kirjoitushetkellä -- ääkköset mukaan lukien.

    Tiedosto luetaan takaisin **UTF-8:na ja tavuina**: väärä koodaus tuottaisi
    tiedoston, joka avautuu Windowsilla mutta näyttää Discordissa väärältä, ja
    osamerkkijonoväite ei huomaisi sitä.
    """
    from pappascout.render import render_report

    archive = build_archive(tmp_path)
    result = run(archive)
    entry = render_stage.read_report(archive.report_json(TEAM_KEY), TEAM_KEY)
    expected = render_report(
        entry, round_list_paths=render_stage.round_list_paths(archive, entry)
    )

    written = archive.resolve(result.outputs[0])
    assert written.read_text(encoding="utf-8") == expected
    assert written.read_bytes() == expected.encode("utf-8")
    assert "ä" in expected


def test_the_file_name_carries_the_timestamp_and_the_team_slug(
    tmp_path: Path,
) -> None:
    archive = build_archive(tmp_path)
    result = run(archive, now=STAMP)
    assert result.outputs[0].name == f"2026-08-30T0307-{TEAM_KEY}.md"


def test_running_twice_never_overwrites_the_earlier_report(tmp_path: Path) -> None:
    """Sama minuutti, kaksi ajoa: uusi tiedosto, vanha säilyy koskemattomana."""
    archive = build_archive(tmp_path)

    first = run(archive, now=STAMP)
    first_path = archive.resolve(first.outputs[0])
    original = first_path.read_text(encoding="utf-8")

    second = run(archive, now=STAMP)
    second_path = archive.resolve(second.outputs[0])

    assert first_path != second_path
    assert second_path.name == f"2026-08-30T0307-{TEAM_KEY}-02.md"
    assert first_path.is_file()
    assert first_path.read_text(encoding="utf-8") == original

    third = run(archive, now=STAMP)
    assert archive.resolve(third.outputs[0]).name.endswith("-03.md")
    assert len(reports(archive)) == 3


def test_the_ordinal_is_zero_padded_so_the_listing_sorts(tmp_path: Path) -> None:
    """Ilman täyttöä hakemistolistaus järjestäisi ``-10, -100, -11, -2``."""
    archive = build_archive(tmp_path)
    for _ in range(11):
        run(archive, now=STAMP)
    numbered = [
        path.name
        for path in reports(archive)
        if path.name != f"2026-08-30T0307-{TEAM_KEY}.md"
    ]
    assert len(numbered) == 10
    # Aakkosjarjestys on numerojarjestys vain nollataytettyna: ilman taytetta
    # "-10" tulisi ennen "-2":ta.
    assert numbered == sorted(numbered)
    assert numbered[0].endswith("-02.md")
    assert numbered[-1].endswith("-11.md")


def test_a_full_minute_of_reports_fails_instead_of_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nimien loppuminen on virhe, ei syy ylikirjoittaa vanhaa raporttia."""
    monkeypatch.setattr(render_stage, "MAX_REPORTS_PER_MINUTE", 2)
    archive = build_archive(tmp_path)
    run(archive, now=STAMP)
    run(archive, now=STAMP)
    with pytest.raises(PappascoutError, match="ei ylikirjoiteta"):
        run(archive, now=STAMP)
    assert len(reports(archive)) == 2


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    run(archive)
    assert not has_temp_leftovers(archive.root)


def test_report_name_numbering_starts_without_a_suffix() -> None:
    assert report_name("2026-08-30T0307", "abc") == "2026-08-30T0307-abc.md"
    assert report_name("2026-08-30T0307", "abc", 2) == "2026-08-30T0307-abc-02.md"
    assert report_name("2026-08-30T0307", "abc", 12) == "2026-08-30T0307-abc-12.md"
    for bad in (0, -1, MAX_REPORTS_PER_MINUTE + 1):
        with pytest.raises(ValueError, match="järjestysluvun"):
            report_name("2026-08-30T0307", "abc", bad)


# --- Varaus ja epäonnistunut kirjoitus ------------------------------------------


def test_a_failed_write_does_not_leave_an_empty_report_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Varaus on tyhjä tiedosto; jos kirjoitus kaatuu, se on peruttava.

    Ilman perumista hakemistoon jäisi pysyvästi nollatavuinen ``.md``, joka
    näyttää raportilta, vie järjestysluvun eikä ole atomisen kirjoituksen
    väliaikaistiedosto -- eli sitä ei löydä mikään siivousta etsivä tarkistus.
    """

    def boom(*args, **kwargs):
        raise OSError("levy täynnä")

    monkeypatch.setattr(render_stage, "atomic_write_text", boom)
    archive = build_archive(tmp_path)

    with pytest.raises(PappascoutError, match="Varaus peruttiin"):
        run(archive, now=STAMP)

    assert reports(archive) == []
    assert not has_temp_leftovers(archive.root)


def test_a_write_failure_frees_the_name_for_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peruttu varaus ei saa viedä järjestyslukua seuraavalta ajolta."""
    calls = {"n": 0}
    real = render_stage.atomic_write_text

    def once(path, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("levy täynnä")
        return real(path, text)

    monkeypatch.setattr(render_stage, "atomic_write_text", once)
    archive = build_archive(tmp_path)
    with pytest.raises(PappascoutError):
        run(archive, now=STAMP)
    result = run(archive, now=STAMP)
    assert result.outputs[0].name == f"2026-08-30T0307-{TEAM_KEY}.md"


def test_an_unwritable_directory_is_a_finnish_error_not_a_stack_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vain ``FileExistsError`` tarkoittaa "nimi varattu"; muu on virhe."""
    archive = build_archive(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError("ei oikeuksia")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(PappascoutError, match="ei voitu varata"):
        run(archive, now=STAMP)


def test_an_uncreatable_directory_is_a_finnish_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = build_archive(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError("ei oikeuksia")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(PappascoutError, match="ei voitu luoda"):
        run(archive, now=STAMP)


# --- Syötteen tarkistus ---------------------------------------------------------


def test_a_missing_report_json_tells_the_user_to_aggregate(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    archive.report_json(TEAM_KEY).unlink()
    with pytest.raises(PappascoutError, match="aggregate"):
        render_stage.read_report(archive.report_json(TEAM_KEY), TEAM_KEY)


def test_a_different_schema_version_refuses_and_says_to_aggregate(
    tmp_path: Path,
) -> None:
    """Vanha skeemaversio: ajo kieltäytyy eikä muotoile puolikasta raporttia."""
    archive = build_archive(tmp_path)
    path = archive.report_json(TEAM_KEY)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = "0.9.0"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PappascoutError) as excinfo:
        run(archive)
    message = str(excinfo.value)
    assert "skeemaversio" in message
    assert "0.9.0" in message
    assert "aggregate" in message
    assert reports(archive) == []


def test_a_broken_report_json_is_a_clear_error_not_a_stack_trace(
    tmp_path: Path,
) -> None:
    archive = build_archive(tmp_path)
    archive.report_json(TEAM_KEY).write_text("{ ei jsonia", encoding="utf-8")
    with pytest.raises(PappascoutError, match="JSONina"):
        run(archive)


def test_a_report_that_does_not_match_the_model_is_rejected(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    path = archive.report_json(TEAM_KEY)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sample"]["rounds"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PappascoutError, match="ei vastaa raporttimallia"):
        run(archive)


def test_a_report_belonging_to_another_team_is_refused(tmp_path: Path) -> None:
    """``team.key`` ja hakemiston nimi ovat sama asia -- tai tiedosto on väärässä.

    Ero tarkoittaa, että raportti nimettäisiin hakemiston mukaan mutta
    kierrosliitteen polut ja tilastot kertoisivat toisesta joukkueesta.
    """
    archive = build_archive(tmp_path)
    path = archive.report_json(TEAM_KEY)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["team"]["key"] = OTHER_TEAM
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PappascoutError, match="koskee joukkuetta"):
        run(archive)
    assert reports(archive) == []


# --- Joukkueen valinta ----------------------------------------------------------


def test_team_keys_lists_only_aggregated_teams(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    (archive.root / "aggregates" / "tyhja").mkdir(parents=True)
    assert render_stage.team_keys(archive) == [TEAM_KEY]


def test_a_unique_prefix_is_enough(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    assert render_stage.resolve_team(archive, TEAM_KEY[:6]) == TEAM_KEY


def test_a_prefix_that_matches_nothing_lists_the_candidates(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    with pytest.raises(PappascoutError, match="ei täsmää yhteenkään"):
        render_stage.resolve_team(archive, "zz")


def test_a_missing_team_option_lists_the_candidates(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    with pytest.raises(PappascoutError, match="Kerro --team"):
        render_stage.resolve_team(archive, None)


@pytest.mark.parametrize("empty", ["", "   ", "\t"])
def test_an_empty_team_is_not_a_prefix_that_matches_everything(
    tmp_path: Path, empty: str
) -> None:
    """Jokainen tunniste alkaa tyhjällä merkkijonolla.

    Ilman tarkistusta ``--team ""`` valitsisi hiljaa ainoan joukkueen -- eli
    tekisi juuri sen, minkä ``--team``in vaatiminen estää.
    """
    archive = build_archive(tmp_path)
    with pytest.raises(PappascoutError, match="tyhjä"):
        render_stage.resolve_team(archive, empty)


def test_a_prefix_matching_two_teams_is_refused(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path,
        teams={"aaaa1111": report([pistol_map()]), "aaaa2222": report([pistol_map()])},
    )
    with pytest.raises(PappascoutError, match="useampaan"):
        render_stage.resolve_team(archive, "aaaa")


def test_an_empty_archive_says_to_aggregate_first(tmp_path: Path) -> None:
    archive = ArchivePaths(root=tmp_path / "tyhja")
    with pytest.raises(PappascoutError, match="aggregate --team"):
        render_stage.resolve_team(archive, TEAM_KEY)


# --- Kierrosliitteen polut ------------------------------------------------------


def test_round_list_paths_are_absolute_and_come_from_archive_paths(
    tmp_path: Path,
) -> None:
    """Discordiin liitetystä raportista lukija ei näe missä arkiston juuri on."""
    archive = build_archive(tmp_path)
    entry = render_stage.read_report(archive.report_json(TEAM_KEY), TEAM_KEY)
    paths = render_stage.round_list_paths(archive, entry)
    assert paths == [str(archive.classified_round_list(TEAM_KEY, DEMO_ID))]
    assert Path(paths[0]).is_absolute()


def test_the_written_report_contains_those_paths(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    result = run(archive)
    text = archive.resolve(result.outputs[0]).read_text(encoding="utf-8")
    assert str(archive.classified_round_list(TEAM_KEY, DEMO_ID)) in text


# --- Manifesti ------------------------------------------------------------------


def manifest_of(archive: ArchivePaths, result) -> Manifest:
    return Manifest.read(archive.resolve(result.manifest_path))


def test_the_manifest_records_the_input_the_parameters_and_the_output(
    tmp_path: Path,
) -> None:
    archive = build_archive(tmp_path)
    result = run(archive)

    manifest = manifest_of(archive, result)
    assert manifest.stage == "render"
    assert manifest.status == "ok"
    assert manifest.outputs == [str(result.outputs[0])]
    assert manifest.inputs[0].result_id == f"aggregates/{TEAM_KEY}"
    assert manifest.inputs[0].sha256
    assert "jinja2" in manifest.tool_versions


def test_each_report_gets_its_own_manifest(tmp_path: Path) -> None:
    """Yhteinen manifesti kestäisi huonosti juuri sitä rinnakkaisuutta,
    jonka varalta nimi varataan: viimeisenä kirjoittava jäisi voimaan ja
    kuvaisi eri raporttia kuin se, jonka käyttäjä juuri sai."""
    archive = build_archive(tmp_path)
    first = run(archive, now=STAMP)
    second = run(archive, now=STAMP)

    assert first.manifest_path != second.manifest_path
    assert manifest_of(archive, first).outputs == [str(first.outputs[0])]
    assert manifest_of(archive, second).outputs == [str(second.outputs[0])]
    assert first.manifest_path.name == f"2026-08-30T0307-{TEAM_KEY}.manifest.json"


def test_a_missing_aggregate_manifest_does_not_stop_the_report(
    tmp_path: Path,
) -> None:
    """Raportti on tärkeämpi kuin jäljitettävyys; syöte merkitään tuntemattomaksi."""
    archive = build_archive(tmp_path, write_manifest=False)
    result = run(archive)
    assert archive.resolve(result.outputs[0]).is_file()
    assert manifest_of(archive, result).inputs[0].sha256 == ""


def test_the_template_is_part_of_the_parameter_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mallin muokkaaminen muuttaa raporttia, joten se näkyy manifestissa."""
    archive = build_archive(tmp_path)
    before = manifest_of(archive, run(archive)).params_hash

    monkeypatch.setattr(render_stage, "template_digest", lambda: "f" * 64)
    after = manifest_of(archive, run(archive)).params_hash
    assert before != after


def test_the_stage_is_never_skipped(tmp_path: Path) -> None:
    """Käyttäjä pyysi raportin; ohitus jättäisi hänet ilman tiedostoa."""
    archive = build_archive(tmp_path)
    first = run(archive)
    second = run(archive)
    assert not first.skipped
    assert not second.skipped
    assert first.outputs != second.outputs


# --- Tuloksen luvut -------------------------------------------------------------


def test_stats_carry_the_numbers_the_user_checks(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    stats = run(archive).stats
    assert stats["team_key"] == TEAM_KEY
    assert stats["team_name_known"] is True
    assert stats["demos"] == 1
    assert stats["rounds"] == 2
    assert stats["maps"] == ["de_ancient"]
    assert stats["lines"] > 0


def test_stats_flag_a_team_without_a_name(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path, teams={TEAM_KEY: report([pistol_map()], display_name=TEAM_KEY)}
    )
    assert run(archive).stats["team_name_known"] is False
