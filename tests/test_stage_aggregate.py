"""``stages.aggregate`` -- vaiheen testit.

Vaihe ei lue demoa, joten sen koko logiikka -- joukkueen kokoaminen, taulujen
luku, atominen kirjoitus, manifesti ja ohitus -- testataan käsin rakennetuilla
tauluilla väliaikaisessa arkistossa. Ainoat demoa vaativat testit ovat lopun
regressiot, ja ne ohittavat itsensä siististi.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from time import sleep

import polars as pl
import pytest

from conftest import LEAGUE_DEMOS, has_temp_leftovers, require_demo
from pappascout.archive.manifest import Manifest, ManifestInput
from pappascout.archive.paths import ArchivePaths
from pappascout.cli import _render_aggregate
from pappascout.domain.models import load_settings
from pappascout.domain.report import Report

SRC = Path(__file__).resolve().parents[1] / "src" / "pappascout"

#: Kentät, joita vaihe lukee asetusosioistaan. Luetaan lähdekoodista,
#: jotta hashattu luettelo ei voi vanheta hiljaa.
THRESHOLD_READ = r"\bthresholds\.([a-z_]+)"
LEAGUE_READ = r"\bleague\.([a-z_]+)"
from pappascout.errors import AggregateError, PappascoutError, SchemaError
from pappascout.stages import aggregate as aggregate_stage
from test_aggregate import (
    OPPONENT,
    TEAM,
    aggregate_settings,
    classified_frame,
    classified_row,
    event_rows,
    events_frame,
    tick_row,
    ticks_frame,
    thresholds,
)


# --- Arkiston rakennus ----------------------------------------------------------


def build_archive(
    tmp_path: Path,
    demos: dict[str, str],
    *,
    rounds: int = 2,
    players: int = 5,
    write_parsed: bool = True,
    write_manifest: bool = True,
    opponent_players: int = 5,
) -> ArchivePaths:
    """Rakenna arkisto, jossa on annetut demot annettujen kokoonpanojen alla.

    Args:
        demos: ``map_demo_id -> lineup_key``.
        rounds: Kierroksia per demo.
        players: Kohdejoukkueen pelaajien määrä näytepistetaulussa.
        write_parsed: Kirjoitetaanko ``parsed/``-taulut. ``False`` tuottaa
            puuttuvan demon.
        write_manifest: Kirjoitetaanko luokittelun manifesti.
        opponent_players: Vastustajan pelaajien määrä; heidän rivinsä eivät saa
            päätyä raporttiin.
    """
    archive = ArchivePaths(root=tmp_path / "arkisto")
    for demo, lineup in demos.items():
        classified = classified_frame(
            [
                classified_row(demo, n, round_type="pistol" if n == 1 else "full")
                for n in range(1, rounds + 1)
            ]
        )
        path = archive.classified(lineup, demo)
        path.parent.mkdir(parents=True, exist_ok=True)
        classified.write_parquet(path)

        if write_manifest:
            Manifest.new(
                result_id=f"classified/{lineup}/{demo}",
                stage="classify",
                params_hash="hash",
                inputs=[ManifestInput(result_id=f"parsed/{demo}", sha256="x")],
                outputs=(f"classified/{lineup}/{demo}.parquet",),
            ).write(archive.classified_manifest(lineup, demo))

        if not write_parsed:
            continue
        ticks = ticks_frame(
            [
                tick_row(demo, n, f"{lineup}-p{i}", "BombsiteA", lineup=lineup)
                for n in range(1, rounds + 1)
                for i in range(players)
            ]
            + [
                tick_row(
                    demo,
                    n,
                    f"{OPPONENT}-p{i}",
                    "BombsiteB",
                    lineup=OPPONENT,
                    side="CT",
                )
                for n in range(1, rounds + 1)
                for i in range(opponent_players)
            ]
        )
        events = events_frame(
            event_rows(demo, 1, 0, "smoke", lineup=lineup)
            + event_rows(demo, 1, 1, "he", lineup=OPPONENT, side="CT")
        )
        ticks_path = archive.parsed_table(demo, "ticks")
        ticks_path.parent.mkdir(parents=True, exist_ok=True)
        ticks.write_parquet(ticks_path)
        events.write_parquet(archive.parsed_table(demo, "events"))
    return archive


def run(archive: ArchivePaths, team: str | None = TEAM, **kwargs):
    kwargs.setdefault("aggregate_settings", aggregate_settings())
    return aggregate_stage.run(thresholds(), _league(), archive, team, **kwargs)


def _league():
    """``[league]``-osio oikeasta asetustiedostosta ilman arkistoa."""
    from conftest import REAL_SETTINGS

    return load_settings(REAL_SETTINGS, env_files=()).league


def read_report(archive: ArchivePaths, team: str = TEAM) -> Report:
    return Report.model_validate_json(
        archive.report_json(team).read_text(encoding="utf-8")
    )


# --- Perusajo -------------------------------------------------------------------


def test_one_demo_produces_a_report_that_validates(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    result = run(archive)

    assert result.stage == "aggregate"
    assert result.unit == TEAM
    assert result.status == "ok"
    assert not result.skipped
    assert [str(p) for p in result.outputs] == [f"aggregates/{TEAM}/report.json"]

    report = read_report(archive)
    assert report.team.key == TEAM
    assert report.sample.demos == 1
    assert [m.map_name for m in report.maps] == ["de_nuke"]


def test_four_demos_of_the_same_team_become_one_report(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path,
        {
            "Ancient_vs_a": TEAM,
            "Anubis_vs_b": TEAM,
            "inferno_vs_c": TEAM,
            "Nuke_vs_d": TEAM,
        },
    )
    run(archive)
    report = read_report(archive)
    assert report.sample.demos == 4
    assert sorted(m.map_name for m in report.maps) == [
        "de_ancient",
        "de_anubis",
        "de_inferno",
        "de_nuke",
    ]
    assert report.sample.rounds == sum(m.sample.rounds for m in report.maps)


def test_the_opponents_rows_are_filtered_out(tmp_path: Path) -> None:
    """Vastustajan näytepisteet ja kranaatit eivät kuulu tähän raporttiin."""
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM}, players=3, opponent_players=5
    )
    run(archive)
    report = read_report(archive)
    position = report.maps[0].sides[0].round_types[0].positions[0]
    assert {a.area for a in position.areas} == {"BombsiteA"}
    types = {
        c.grenade_type
        for rt in report.maps[0].sides[0].round_types
        for c in rt.utility_counts
    }
    assert types == {"smoke"}


def test_lineups_of_the_same_team_are_joined(tmp_path: Path) -> None:
    """Yksi vaihto tuottaa uuden kokoonpanotunnisteen; demo ei saa kadota."""
    other = "cccccccccccccccc"
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    # Toinen kokoonpano, joka jakaa kolme pelaajaa ensimmäisen kanssa.
    extra = build_archive(tmp_path / "toinen", {"Anubis_vs_b": other})
    for src in (extra.root / "classified").rglob("*"):
        if src.is_file():
            dst = archive.root / src.relative_to(extra.root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    for src in (extra.root / "parsed").rglob("*.parquet"):
        dst = archive.root / src.relative_to(extra.root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    # Kirjoitetaan toisen demon näytepisteet niin, että kolme pelaajaa on samoja.
    shared = ticks_frame(
        [
            tick_row("Anubis_vs_b", n, f"{TEAM}-p{i}", "BombsiteA", lineup=other)
            for n in range(1, 3)
            for i in range(3)
        ]
        + [
            tick_row("Anubis_vs_b", n, f"{other}-p{i}", "BombsiteA", lineup=other)
            for n in range(1, 3)
            for i in range(3, 5)
        ]
    )
    shared.write_parquet(archive.parsed_table("Anubis_vs_b", "ticks"))

    run(archive)
    report = read_report(archive)
    assert sorted(report.team.lineup_keys) == sorted([TEAM, other])
    assert report.sample.demos == 2
    assert len(report.maps) == 2


def test_a_demo_without_parsed_tables_is_reported_missing(tmp_path: Path) -> None:
    """Puuttuva demo ei kaada ajoa eikä katoa hiljaa."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    broken = build_archive(
        tmp_path / "rikki", {"Anubis_vs_b": TEAM}, write_parsed=False
    )
    for src in (broken.root / "classified").rglob("*"):
        if src.is_file():
            dst = archive.root / src.relative_to(broken.root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    result = run(archive)
    assert result.status == "ok"
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Anubis_vs_b"]
    assert "ticks.parquet" in report.missing_demos[0].reason
    assert report.sample.demos == 1


def test_no_usable_demo_is_an_error_that_names_the_reason(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, write_parsed=False)
    # Näytepistetaulu on silti tarpeen kokoonpanon lukemiseen, joten
    # kokoaminen kaatuu jo siihen.
    with pytest.raises(PappascoutError, match="kokoonpanoa ei saatu luettua"):
        run(archive)


def test_missing_classify_manifest_moves_the_demo_to_missing(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    extra = build_archive(
        tmp_path / "ilman", {"Anubis_vs_b": TEAM}, write_manifest=False
    )
    for sub in ("classified", "parsed"):
        for src in (extra.root / sub).rglob("*"):
            if src.is_file():
                dst = archive.root / src.relative_to(extra.root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())

    run(archive)
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Anubis_vs_b"]
    assert "manifesti" in report.missing_demos[0].reason.lower()


# --- Joukkueen valinta ----------------------------------------------------------


def test_a_prefix_is_enough_to_name_the_team(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    assert aggregate_stage.resolve_team(archive, TEAM[:6]) == TEAM


def test_missing_team_lists_the_alternatives(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    with pytest.raises(PappascoutError, match="Arkiston luokitellut joukkueet"):
        run(archive, team=None)


def test_an_unknown_team_is_named_in_the_error(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    with pytest.raises(PappascoutError, match="ei täsmää yhteenkään"):
        run(archive, team="zzzz")


def test_an_empty_archive_says_what_to_run_first(tmp_path: Path) -> None:
    archive = ArchivePaths(root=tmp_path / "tyhja")
    with pytest.raises(PappascoutError, match="classify"):
        run(archive)


def test_team_keys_lists_only_directories_with_results(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    (archive.root / "classified" / "tyhja").mkdir(parents=True)
    assert aggregate_stage.team_keys(archive) == [TEAM]


# --- Manifesti ja ohitus --------------------------------------------------------


def test_a_second_run_is_skipped_and_reports_the_same_numbers(
    tmp_path: Path,
) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    first = run(archive)
    second = run(archive)
    assert not first.skipped
    assert second.skipped
    assert second.stats["rounds"] == first.stats["rounds"]
    assert second.stats["maps"] == first.stats["maps"]


def test_force_runs_again_even_when_the_manifest_matches(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    assert not run(archive, force=True).skipped


def test_a_changed_classify_result_invalidates_the_report(tmp_path: Path) -> None:
    """Syötteen tunniste on manifestin sisällöstä, ei tiedoston tiivisteestä."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    Manifest.new(
        result_id=f"classified/{TEAM}/Nuke_vs_a",
        stage="classify",
        params_hash="toinen-hash",
        inputs=[ManifestInput(result_id="parsed/Nuke_vs_a", sha256="x")],
        outputs=(f"classified/{TEAM}/Nuke_vs_a.parquet",),
    ).write(archive.classified_manifest(TEAM, "Nuke_vs_a"))
    assert not run(archive).skipped


def test_a_deleted_report_is_written_again(tmp_path: Path) -> None:
    """OneDrive: pieni manifesti ehtii synkata ennen tulosta."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    archive.report_json(TEAM).unlink()
    assert not run(archive).skipped
    assert archive.report_json(TEAM).is_file()


def test_a_report_from_an_older_schema_is_written_again(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    archive.report_json(TEAM).write_text('{"vanha": true}', encoding="utf-8")
    assert not run(archive).skipped
    assert read_report(archive).sample.demos == 1


def test_the_manifest_records_every_demo_as_an_input(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM}
    )
    run(archive)
    manifest = Manifest.read(archive.report_manifest(TEAM))
    assert manifest.stage == "aggregate"
    assert sorted(i.result_id for i in manifest.inputs) == [
        f"classified/{TEAM}/Anubis_vs_b",
        f"classified/{TEAM}/Nuke_vs_a",
    ]
    assert manifest.tool_versions == {}


def test_thresholds_change_the_params_hash(tmp_path: Path) -> None:
    """Kynnysten säätö ajaa aggregoinnin uudelleen mutta ei parsintaa."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    before = Manifest.read(archive.report_manifest(TEAM)).params_hash
    aggregate_stage.run(
        thresholds(small_sample_rounds=9),
        _league(),
        archive,
        TEAM,
        aggregate_settings=aggregate_settings(),
    )
    assert Manifest.read(archive.report_manifest(TEAM)).params_hash != before


# --- Kirjoitus ------------------------------------------------------------------


def test_the_write_leaves_no_temporary_files(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    assert not has_temp_leftovers(archive.root)


def test_the_report_is_valid_utf8_json(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    data = json.loads(archive.report_json(TEAM).read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0.0"
    assert data["team"]["roster_source"] == "lineups"


def test_a_corrupt_classified_table_is_named(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    pl.DataFrame({"vaara": [1]}).write_parquet(
        archive.classified(TEAM, "Nuke_vs_a")
    )
    with pytest.raises(SchemaError, match="classify"):
        run(archive)


# --- Katselmuksen löydökset -----------------------------------------------------


def test_a_report_that_no_longer_passes_the_sample_check_is_written_again(
    tmp_path: Path,
) -> None:
    """Summavirhe on AggregateError eikä ValueError.

    Vanha ``report.json``, joka ei enää läpäise summavalidaattoreita, ei saa
    kaataa ohitushaaraa ikuisesti -- vaihe kirjoittaa tilalle uuden.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    broken = json.loads(archive.report_json(TEAM).read_text(encoding="utf-8"))
    broken["sample"]["rounds"] = 999
    broken["sample"]["unknown"]["rounds"] = 999
    archive.report_json(TEAM).write_text(
        json.dumps(broken, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(AggregateError):
        Report.model_validate(broken)

    result = run(archive)
    assert not result.skipped
    assert read_report(archive).sample.rounds == 2


def test_a_report_from_a_foreign_schema_version_is_written_again(
    tmp_path: Path,
) -> None:
    """Skeemaversio on verrattava, kuten ``Manifest`` tekee.

    Vanha tiedosto voi validoitua kenttä kentältä ja tarkoittaa silti eri
    asiaa; ilman vertailua ohitus palauttaisi sen luvut tämän ajon tuloksena.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    stale = json.loads(archive.report_json(TEAM).read_text(encoding="utf-8"))
    stale["schema_version"] = "0.1.0"
    stale["unclassified_rounds"] = 999
    archive.report_json(TEAM).write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8"
    )

    result = run(archive)
    assert not result.skipped
    assert result.stats["unclassified"] == 0
    assert read_report(archive).schema_version == "1.0.0"


def test_the_real_stats_render_without_a_key_error(tmp_path: Path) -> None:
    """Vaiheen ja tulosteen sopimusta ei valvo mikään muu testi.

    Jokainen CLI-testi rakentaa statsit käsin ja jokainen vaihetesti vertaa
    tuottajaa itseensä, joten avaimen uudelleennimeäminen menisi läpi
    vihreällä testisarjalla ja kaatuisi vasta oikeassa ajossa.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM})
    text = _render_aggregate(run(archive))
    assert "Otanta" in text
    assert "de_nuke" in text and "de_anubis" in text
    assert _render_aggregate(run(archive)).startswith("Ohitettu:")


def test_the_summary_reports_the_roster_size(tmp_path: Path) -> None:
    """Rosteri-rivi oli tulosteessa ilman testiä."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, players=5)
    assert "5 pelaajaa havaittu" in _render_aggregate(run(archive))


def test_a_lineup_that_cannot_be_read_at_all_is_still_reported(
    tmp_path: Path,
) -> None:
    """Kokoonpano, jota ei voi liittää, ei saa kadota jäljettömiin."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    orphan = build_archive(
        tmp_path / "orpo", {"Anubis_vs_b": "cccccccccccccccc"}, write_parsed=False
    )
    for src in (orphan.root / "classified").rglob("*"):
        if src.is_file():
            dst = archive.root / src.relative_to(orphan.root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    run(archive)
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Anubis_vs_b"]
    assert "ei voitu liittää joukkueeseen" in report.missing_demos[0].reason


def test_many_teams_in_the_archive_do_not_confuse_the_identity(
    tmp_path: Path,
) -> None:
    """Identiteetti ratkaistaan kaikkia kokoonpanoja vasten.

    Arkistossa on neljä joukkuetta, joista yksikään muu ei jaa pelaajia
    kohteen kanssa -- joten liittäminen ei saa vetää niitä mukaan.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    for index, name in enumerate(("Anubis_vs_b", "inferno_vs_c", "Ancient_vs_d")):
        other = chr(ord("c") + index) * 16
        extra = build_archive(tmp_path / ("muu" + str(index)), {name: other})
        for sub in ("classified", "parsed"):
            for src in (extra.root / sub).rglob("*"):
                if src.is_file():
                    dst = archive.root / src.relative_to(extra.root)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())

    assert len(aggregate_stage.team_keys(archive)) == 4
    run(archive)
    report = read_report(archive)
    assert report.team.lineup_keys == [TEAM]
    assert report.sample.demos == 1
    assert report.missing_demos == []


def test_the_report_names_the_thresholds_the_rounds_were_classified_with(
    tmp_path: Path,
) -> None:
    """Luokittelun kynnykset luetaan taulusta, ei nykyisistä asetuksista."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    report = read_report(archive)
    assert report.classify_thresholds["full_equip_min"] == 4000
    assert report.thresholds_used["aggregate"]["utility_seconds_buckets"] == [
        5.0,
        10.0,
        20.0,
    ]


def test_an_unrelated_threshold_does_not_invalidate_the_report(
    tmp_path: Path,
) -> None:
    """Parametrihash vain siitä, mikä muuttaa tämän vaiheen tulosta."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    before = Manifest.read(archive.report_manifest(TEAM)).params_hash
    aggregate_stage.run(
        thresholds(full_equip_min=4500),
        _league(),
        archive,
        TEAM,
        aggregate_settings=aggregate_settings(),
    )
    assert Manifest.read(archive.report_manifest(TEAM)).params_hash == before


def test_the_time_windows_do_change_the_params_hash(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    before = Manifest.read(archive.report_manifest(TEAM)).params_hash
    aggregate_stage.run(
        thresholds(),
        _league(),
        archive,
        TEAM,
        aggregate_settings=aggregate_settings(utility_seconds_buckets=[7.0]),
    )
    assert Manifest.read(archive.report_manifest(TEAM)).params_hash != before


def test_every_setting_the_stage_reads_is_in_the_params_hash() -> None:
    """Nimetty luettelo ei saa vanheta hiljaa.

    Luetaan lähdekoodista, mitä ``thresholds.``- ja ``league.``-kenttiä vaihe
    ja sen domain-funktiot lukevat, ja verrataan hashattuun luetteloon. Jos
    joku lisää uuden luetun kentän muttei hashiin, raportti jäisi
    vanhentuneeksi ilman että mikään kertoisi siitä.
    """
    source = "".join(
        (SRC / name).read_text(encoding="utf-8")
        for name in ("stages/aggregate.py", "domain/aggregate.py")
    )
    ignore = {"model_dump"}
    read = set(re.findall(THRESHOLD_READ, source)) - ignore
    assert read <= set(aggregate_stage.HASHED_THRESHOLD_KEYS), read
    league_read = set(re.findall(LEAGUE_READ, source)) - ignore
    assert league_read <= set(aggregate_stage.HASHED_LEAGUE_KEYS), league_read


def test_the_manifest_fingerprint_ignores_only_the_timestamp() -> None:
    """Sopimus "luontihetki jätetään pois" oli vain välillisesti testattu."""

    def make(params_hash: str = "h") -> Manifest:
        return Manifest.new(
            result_id="classified/x/y",
            stage="classify",
            params_hash=params_hash,
            inputs=[ManifestInput(result_id="parsed/y", sha256="s")],
            outputs=("classified/x/y.parquet",),
        )

    first = make()
    sleep(0.002)
    second = make()
    assert first.created_at != second.created_at
    assert first.fingerprint() == second.fingerprint()
    assert make("toinen").fingerprint() != first.fingerprint()


def test_a_table_written_with_a_different_column_order_still_reads(
    tmp_path: Path,
) -> None:
    """Sarakejärjestys ei ole osa sopimusta, mutta ``pl.concat`` välittää.

    ``validate`` hyväksyy minkä tahansa järjestyksen -- sopimus on nimistä ja
    tyypeistä -- joten kahdella eri versiolla kirjoitettu arkisto voi sisältää
    saman taulun eri järjestyksessä. Ilman järjestämistä ``pl.concat``
    kaatuisi englanninkieliseen ``ShapeError``iin, joka ei kerro käyttäjälle
    mitään.
    """
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM}
    )
    for demo in ("Anubis_vs_b",):
        for table in ("ticks", "events"):
            path = archive.parsed_table(demo, table)
            df = pl.read_parquet(path)
            df.select(reversed(df.columns)).write_parquet(path)
        path = archive.classified(TEAM, demo)
        df = pl.read_parquet(path)
        df.select(reversed(df.columns)).write_parquet(path)

    run(archive)
    assert read_report(archive).sample.demos == 2


# --- Regressiot oikeilla demoilla -----------------------------------------------


@pytest.mark.demo
def test_the_league_demos_aggregate_into_one_team(tmp_path: Path) -> None:
    """Neljä MatureMayhem-demoa, yksi raportti, otanta ``unknown``.

    Testi ajaa koko putken (parse -> classify -> aggregate) väliaikaiseen
    arkistoon, joten se ei nojaa siihen, mitä kehittäjän omassa arkistossa
    sattuu olemaan. Se on myös ainoa paikka, jossa kokoonpanojen liittäminen
    todennetaan oikealla aineistolla: MatureMayhem on näissä neljässä demossa
    kahden eri kokoonpanotunnisteen alla.
    """
    from pappascout.stages import classify as classify_stage
    from pappascout.stages import parse as parse_stage

    demos = [require_demo(name) for name, _ in LEAGUE_DEMOS]
    settings = load_settings(_real_settings_at(tmp_path), env_files=())
    archive = ArchivePaths.from_settings(settings.project.archive_root)

    members: dict[str, dict[str, set[str]]] = {}
    for demo in demos:
        map_demo_id = demo.stem
        parse_stage.run(
            settings.parse,
            archive,
            map_demo_id,
            parse_stage.default_parser(settings.parse),
            demo_path=demo,
        )
        members[map_demo_id] = _lineup_members(archive, map_demo_id)

    subject = _common_lineups(members, settings.thresholds.team_identity_min_common)
    assert len(subject) == len(demos), (
        "Jokaisessa demossa pitäisi olla sama joukkue; löytyi " f"{subject}"
    )

    for map_demo_id, lineup in subject.items():
        classify_stage.run(
            settings.thresholds,
            settings.league,
            archive,
            map_demo_id,
            lineup,
            economy=settings.economy,
        )

    result = aggregate_stage.run(
        settings.thresholds,
        settings.league,
        archive,
        next(iter(subject.values())),
        aggregate_settings=settings.aggregate,
    )
    report = Report.model_validate_json(
        archive.report_json(result.unit).read_text(encoding="utf-8")
    )
    assert report.sample.demos == 4
    assert report.sample.unknown.demos == 4
    assert report.sample.league.demos == 0
    assert report.sample.other.demos == 0
    assert {m.map_name for m in report.maps} == {
        "de_ancient",
        "de_anubis",
        "de_inferno",
        "de_nuke",
    }
    for entry in report.maps:
        for side in entry.sides:
            for round_type in side.round_types:
                for position in round_type.positions:
                    for area in position.areas:
                        assert sum(p.n for p in area.players_dist) == area.m


def _real_settings_at(tmp_path: Path) -> Path:
    from conftest import settings_text

    target = tmp_path / "settings.toml"
    target.write_text(settings_text(tmp_path / "arkisto"), encoding="utf-8")
    return target


def _lineup_members(
    archive: ArchivePaths, map_demo_id: str
) -> dict[str, set[str]]:
    df = pl.read_parquet(
        archive.parsed_table(map_demo_id, "ticks"),
        columns=["lineup_key", "player_id"],
    ).unique()
    found: dict[str, set[str]] = {}
    for row in df.iter_rows(named=True):
        found.setdefault(row["lineup_key"], set()).add(row["player_id"])
    return found


def _common_lineups(
    members: dict[str, dict[str, set[str]]], min_common: int
) -> dict[str, str]:
    """Demo -> se kokoonpano, joka esiintyy kaikissa demoissa.

    Kokoonpanotunniste vaihtuu, jos joukkue vaihtaa pelaajaa, joten vertailu
    on tehtävä pelaajajoukoilla eikä tunnisteilla.
    """
    first = next(iter(members))
    for candidate, players in members[first].items():
        picked = {first: candidate}
        for demo, lineups in members.items():
            if demo == first:
                continue
            match = [
                key
                for key, others in lineups.items()
                if len(players & others) >= min_common
            ]
            if len(match) == 1:
                picked[demo] = match[0]
        if len(picked) == len(members):
            return picked
    return {}
