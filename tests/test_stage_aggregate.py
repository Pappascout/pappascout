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
from pappascout.domain.schemas import ARMORED_COLUMN

SRC = Path(__file__).resolve().parents[1] / "src" / "pappascout"

#: Kentät, joita vaihe lukee asetusosioistaan. Luetaan lähdekoodista,
#: jotta hashattu luettelo ei voi vanheta hiljaa.
THRESHOLD_READ = r"\bthresholds\.([a-z_]+)"
LEAGUE_READ = r"\bleague\.([a-z_]+)"
from pappascout.errors import AggregateError, PappascoutError, SchemaError
from pappascout.stages import aggregate as aggregate_stage
from test_aggregate import (
    OPPONENT,
    OPPONENT_CLAN,
    TEAM,
    TEAM_CLAN,
    aggregate_settings,
    classified_frame,
    classified_row,
    death_row,
    deaths_frame,
    event_rows,
    events_frame,
    lineup_row,
    lineups_frame,
    match_frame,
    report_for,
    round_row,
    rounds_frame,
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
    clan: str | None = TEAM_CLAN,
    clan_by_demo: dict[str, str | None] | None = None,
    player_names: bool = True,
    bench_player: str | None = None,
    map_names: dict[str, str | None] | None = None,
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
        clan: Kohdejoukkueen klaaninimi kokoonpanotaulussa. ``None`` = nimeä ei
            havaittu, jolloin raportin on puhuttava tunnisteesta.
        clan_by_demo: Demokohtainen poikkeus ``clan``iin -- nimiristiriidan
            rakentamiseen.
        player_names: Kirjoitetaanko pelaajille nimet. ``False`` jättää ne
            tyhjiksi, jolloin rosterissa on pelkkä SteamID.
        bench_player: Pelaaja, joka on **vain kokoonpanotaulussa** eikä
            yhdelläkään näytepisteellä. Juuri tällainen pelaaja katoaisi, jos
            kokoonpanot luettaisiin ``ticks``-taulusta -- ja se on koko syy
            sille, että ne luetaan ``lineups``-taulusta.
        map_names: ``map_demo_id -> kartan nimi otsikossa``. Oletus on, ettei
            otsikossa ole nimeä (``None``), jolloin nimi päätellään
            tunnisteesta kuten ennen Story 2.11:tä -- niin vanhat testit
            mittaavat yhä päättelyä ja uudet havaintoa.
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
        # Kaksi kuolemaa kierroksella 1: yksi omalle pelaajalle (oma kuolema)
        # ja yksi vastustajalle (oma tappo). Molemmat tarvitaan, koska vaihe
        # suodattaa kahdesta eri sarakkeesta -- yhdellä rivillä toinen
        # suodatin jäisi todentamatta.
        deaths = deaths_frame(
            [
                death_row(
                    demo,
                    1,
                    victim=f"{lineup}-p0",
                    victim_lineup=lineup,
                    attacker=f"{OPPONENT}-p0",
                    attacker_lineup=OPPONENT,
                ),
                death_row(
                    demo,
                    1,
                    victim=f"{OPPONENT}-p1",
                    victim_lineup=OPPONENT,
                    victim_side="CT",
                    victim_area="BombsiteB",
                    attacker=f"{lineup}-p1",
                    attacker_lineup=lineup,
                    attacker_side="T",
                    attacker_area="Middle",
                    t_s=30.0,
                ),
            ]
        )
        # Kierrostaulu: kaksi riviä per kierros, yksi kummallekin
        # joukkueelle -- kuten parse sen kirjoittaa. Vain panssarilaskuri
        # luetaan täältä, mutta molemmat rivit ovat mukana, jotta
        # kokoonpanosuodatus on oikeasti testattavana.
        rounds_table = rounds_frame(
            [
                round_row(demo, n, lineup=lineup, side="T", armored=5)
                for n in range(1, rounds + 1)
            ]
            + [
                round_row(demo, n, lineup=OPPONENT, side="CT", armored=0)
                for n in range(1, rounds + 1)
            ]
        )
        demo_clan = (clan_by_demo or {}).get(demo, clan)
        lineups = lineups_frame(
            [
                lineup_row(
                    demo,
                    f"{lineup}-p{i}",
                    lineup=lineup,
                    player_name=f"nimi{i}" if player_names else None,
                    clan_name=demo_clan,
                )
                for i in range(players)
            ]
            + (
                [
                    lineup_row(
                        demo,
                        bench_player,
                        lineup=lineup,
                        player_name="penkki" if player_names else None,
                        clan_name=demo_clan,
                    )
                ]
                if bench_player
                else []
            )
            + [
                lineup_row(
                    demo,
                    f"{OPPONENT}-p{i}",
                    lineup=OPPONENT,
                    player_name=f"vastus{i}" if player_names else None,
                    clan_name=OPPONENT_CLAN,
                )
                for i in range(opponent_players)
            ]
        )
        ticks_path = archive.parsed_table(demo, "ticks")
        ticks_path.parent.mkdir(parents=True, exist_ok=True)
        ticks.write_parquet(ticks_path)
        events.write_parquet(archive.parsed_table(demo, "events"))
        lineups.write_parquet(archive.parsed_table(demo, "lineups"))
        deaths.write_parquet(archive.parsed_table(demo, "deaths"))
        rounds_table.write_parquet(archive.parsed_table(demo, "rounds"))
        match_frame(demo, (map_names or {}).get(demo)).write_parquet(
            archive.parsed_table(demo, "match")
        )
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
    # Joukkueidentiteetti luetaan kokoonpanotaulusta (Story 2.6), joten
    # yhteiset pelaajat on kirjoitettava sinne -- ei pelkkiin
    # näytepisteisiin.
    lineups_frame(
        [
            lineup_row(
                "Anubis_vs_b",
                f"{TEAM}-p{i}",
                lineup=other,
                player_name=f"nimi{i}",
            )
            for i in range(3)
        ]
        + [
            lineup_row(
                "Anubis_vs_b",
                f"{other}-p{i}",
                lineup=other,
                player_name=f"nimi{i}",
            )
            for i in range(3, 5)
        ]
    ).write_parquet(archive.parsed_table("Anubis_vs_b", "lineups"))

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
    # ``_demo_unusable`` palauttaa **ensimmäisen** puuttuvan taulun, ja
    # luettelon järjestys on osa käyttäjälle näkyvää sopimusta. Väite nimeää
    # taulun eikä tyydy siihen, että jokin puute mainitaan: yleinen väite
    # menisi läpi vaikka luettelosta katoaisi taulu.
    assert "rounds.parquet" in report.missing_demos[0].reason
    assert report.sample.demos == 1


@pytest.mark.parametrize(
    "table", ["rounds", "ticks", "events", "lineups", "deaths", "match"]
)
def test_each_required_parsed_table_is_guarded_on_its_own(
    tmp_path: Path, table: str
) -> None:
    """Jokainen kuudesta taulusta nimetään puuttuessaan.

    Yhteinen testi, joka poistaa vain yhden taulun, jättää loput
    vartioimatta: ``_demo_unusable`` palauttaa ensimmäisen puutteen, joten
    luettelon alkupään taulu peittää kaikki sen jälkeiset. Todennettu
    poistamalla ``"ticks"`` luettelosta -- koko sviitti meni läpi.
    """
    archive = build_archive(
        tmp_path, {"Ancient_vs_a": TEAM, "Nuke_vs_b": TEAM}
    )
    archive.parsed_table("Nuke_vs_b", table).unlink()

    run(archive)
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Nuke_vs_b"]
    assert f"{table}.parquet" in report.missing_demos[0].reason
    assert [m.map_name for m in report.maps] == ["de_ancient"]


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
    # Literaali eikä vakio: vakioon vertaaminen olisi tautologia --
    # koodi kirjoitti arvon juuri siitä vakiosta. Kun versio nousee,
    # tämän rivin PITÄÄ kaatua, jotta nosto on tietoinen.
    assert data["schema_version"] == "7.0.0"
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
    assert read_report(archive).schema_version == "7.0.0"


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
    assert "ei tiedetä kuuluuko demo tälle joukkueelle" in (
        report.missing_demos[0].reason
    )


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


# --- Joukkueen ja pelaajien nimet (Story 2.6) -----------------------------------


def test_the_team_name_comes_from_the_lineups_table(tmp_path: Path) -> None:
    """Nimi on havainto demosta, ei johdos tunnisteesta."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)

    report = read_report(archive)
    assert report.team.display_name == TEAM_CLAN
    assert report.team.display_name_source == "clan_name"
    assert report.team.display_name_alternatives == []
    # Tiedostonimen slug seuraa nimeä, jotta raportin nimessä lukee
    # joukkue eikä tiiviste.
    assert report.team.slug == "maturemayhem"
    # Avain ei muutu: se on hakemistorakenne.
    assert report.team.key == TEAM


def test_the_opponents_clan_name_does_not_become_the_title(tmp_path: Path) -> None:
    """Sama demo sisältää molempien joukkueiden rivit."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    report = read_report(archive)
    assert OPPONENT_CLAN not in [
        report.team.display_name,
        *report.team.display_name_alternatives,
    ]


def test_a_team_without_a_clan_name_keeps_the_key_and_says_so(tmp_path: Path) -> None:
    """Ilman havaintoa nimi on tunniste, ja lähde sanoo sen ääneen."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, clan=None)
    run(archive)

    report = read_report(archive)
    assert report.team.display_name == TEAM
    assert report.team.display_name_source == "team_key"
    assert report.team.slug == TEAM


def test_a_clan_name_without_ascii_still_names_its_own_file(tmp_path: Path) -> None:
    """Kyrillinen klaani: nimi otsikkoon, slug tunnisteesta.

    Slugista ei jää yhtään merkkiä, mutta nimi on silti havaittu. Jos
    varapolku olisi jaettu vakio, jokainen tällainen joukkue saisi
    tiedostonimen ``<aikaleima>-joukkue.md`` -- eli raportit törmäisivät
    toisiinsa ja nimi katoaisi tiedostonimestä kokonaan.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, clan="Кибер")
    run(archive)

    team = read_report(archive).team
    assert team.display_name == "Кибер"
    assert team.display_name_source == "clan_name"
    assert team.slug == TEAM
    assert team.slug != "joukkue"


def test_conflicting_clan_names_are_resolved_and_the_rest_listed(
    tmp_path: Path,
) -> None:
    """Kolme demoa yhdellä nimellä, yksi toisella: enemmistö voittaa."""
    archive = build_archive(
        tmp_path,
        {
            "Ancient_vs_a": TEAM,
            "Anubis_vs_b": TEAM,
            "inferno_vs_c": TEAM,
            "Nuke_vs_d": TEAM,
        },
        clan_by_demo={"Nuke_vs_d": "MM Academy"},
    )
    run(archive)

    report = read_report(archive)
    assert report.team.display_name == TEAM_CLAN
    assert report.team.display_name_alternatives == ["MM Academy"]


def test_the_roster_carries_both_the_name_and_the_steamid(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)

    roster = read_report(archive).team.roster
    assert len(roster) == 5
    assert all(entry.player_id.startswith(f"{TEAM}-p") for entry in roster)
    assert sorted(e.display_name for e in roster) == [f"nimi{i}" for i in range(5)]


def test_a_player_without_a_name_is_still_in_the_roster(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, player_names=False)
    run(archive)

    roster = read_report(archive).team.roster
    assert len(roster) == 5
    assert all(entry.display_name is None for entry in roster)


def test_a_demo_without_the_lineups_table_is_reported_missing(
    tmp_path: Path,
) -> None:
    """Puuttuva kokoonpanotaulu ei kaada ajoa mutta ei myöskään katoa."""
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM, "Ancient_vs_b": TEAM}
    )
    archive.parsed_table("Ancient_vs_b", "lineups").unlink()
    run(archive)

    report = read_report(archive)
    reasons = {m.match: m.reason for m in report.missing_demos}
    assert "Ancient_vs_b" in reasons
    assert "lineups.parquet" in reasons["Ancient_vs_b"]
    assert report.sample.demos == 1


def test_a_player_seen_only_in_the_lineups_table_is_in_the_roster(
    tmp_path: Path,
) -> None:
    """Kokoonpanot luetaan ``lineups``-taulusta eikä ``ticks``-taulusta.

    Näytepistetaulusta puuttuu pelaaja, joka ei ehtinyt yhdellekään
    näytepisteelle -- ja ``lineup_key`` on silti laskettu hänet mukaan lukien.
    Jos kokoonpanot luettaisiin näytepisteistä, tunniste ja sen pelaajajoukko
    olisivat eri mieltä, ja rosterista puuttuisi pelaaja joka pelasi kartan.
    """
    bench = f"{TEAM}-penkki"
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, bench_player=bench)
    run(archive)

    roster = read_report(archive).team.roster
    ids = [entry.player_id for entry in roster]
    assert bench in ids
    assert len(roster) == 6

    # Ja hän on aidosti poissa näytepistetaulusta -- muuten testi ei
    # todistaisi mitään lähteen vaihdosta.
    ticks = pl.read_parquet(archive.parsed_table("Nuke_vs_a", "ticks"))
    assert bench not in set(ticks["player_id"])


def test_the_lineup_join_can_rest_on_a_player_who_has_no_sample_points(
    tmp_path: Path,
) -> None:
    """Joukkueiden liitos tehdään kokoonpanotaulun pelaajajoukolla.

    Kolme yhteistä pelaajaa riittää liitokseen. Tässä kaksi niistä on
    pelaajia, joita näytepistetaulussa ei ole lainkaan, joten vanha lähde ei
    löytäisi yhteisiä pelaajia riittävästi eikä liittäisi kokoonpanoja.
    """
    other = "cccccccccccccccc"
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
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

    # Näytepisteissä toisella kokoonpanolla on VAIN omat pelaajansa:
    # yhteisiä ei näy yhtään. Yhteiset kolme ovat pelkästään
    # kokoonpanotaulussa.
    ticks_frame(
        [
            tick_row("Anubis_vs_b", n, f"{other}-p{i}", "BombsiteA", lineup=other)
            for n in range(1, 3)
            for i in range(3, 5)
        ]
    ).write_parquet(archive.parsed_table("Anubis_vs_b", "ticks"))
    lineups_frame(
        [
            lineup_row("Anubis_vs_b", f"{TEAM}-p{i}", lineup=other)
            for i in range(3)
        ]
        + [
            lineup_row("Anubis_vs_b", f"{other}-p{i}", lineup=other)
            for i in range(3, 5)
        ]
    ).write_parquet(archive.parsed_table("Anubis_vs_b", "lineups"))

    run(archive)
    report = read_report(archive)
    assert sorted(report.team.lineup_keys) == sorted([TEAM, other])


# --- Kuolemataulu (Story 2.7) --------------------------------------------------


def test_own_deaths_and_own_kills_both_reach_the_report(tmp_path: Path) -> None:
    """Suodatus on kahdesta sarakkeesta, ja molemmat puolet on säilyttävä.

    Pelkkä ``victim_lineup_key`` pudottaisi omat tapot ja pelkkä
    ``attacker_lineup_key`` omat kuolemat -- ja kumpikin virhe näyttäisi
    raportissa siltä, että joukkue ei tehnyt sitä mitä se teki.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    run(archive)
    entry = read_report(archive).maps[0].sides[0].round_types[0]

    assert entry.deaths.m == 1
    assert [(a.area, a.n) for a in entry.deaths.first_death_areas] == [("Cave", 1)]
    assert entry.deaths.kills_total == 1
    assert [(k.area, k.n) for k in entry.deaths.kills] == [("Middle", 1)]


def test_the_opponents_own_deaths_do_not_become_ours(tmp_path: Path) -> None:
    """Vastustajien keskinäinen kuolema ei kuulu tähän raporttiin."""
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    extra = deaths_frame(
        [
            death_row(
                "Ancient_vs_a",
                1,
                victim=f"{OPPONENT}-p3",
                victim_lineup=OPPONENT,
                victim_side="CT",
                attacker=f"{OPPONENT}-p4",
                attacker_lineup=OPPONENT,
                attacker_side="CT",
                attacker_area="Ramp",
            )
        ]
    )
    path = archive.parsed_table("Ancient_vs_a", "deaths")
    pl.concat([pl.read_parquet(path), extra]).write_parquet(path)

    run(archive, force=True)
    entry = read_report(archive).maps[0].sides[0].round_types[0]
    assert entry.deaths.m == 1
    assert entry.deaths.kills_total == 1
    assert "Ramp" not in [k.area for k in entry.deaths.kills]


def test_a_demo_without_the_deaths_table_is_reported_missing(
    tmp_path: Path,
) -> None:
    """Puuttuva taulu ei kaada ajoa mutta ei myöskään katoa hiljaa."""
    archive = build_archive(
        tmp_path, {"Ancient_vs_a": TEAM, "Nuke_vs_b": TEAM}
    )
    archive.parsed_table("Nuke_vs_b", "deaths").unlink()

    run(archive)
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Nuke_vs_b"]
    assert "deaths.parquet" in report.missing_demos[0].reason
    assert [m.map_name for m in report.maps] == ["de_ancient"]


def test_an_outdated_deaths_table_tells_the_user_to_reparse(
    tmp_path: Path,
) -> None:
    """Vanhalla versiolla kirjoitettu taulu ei mene läpi hiljaa."""
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    path = archive.parsed_table("Ancient_vs_a", "deaths")
    pl.read_parquet(path).drop("attacker_area").write_parquet(path)

    with pytest.raises(SchemaError) as exc:
        run(archive)
    assert "attacker_area" in str(exc.value)
    assert "uv run pappascout parse Ancient_vs_a --pakota" in str(exc.value)


def test_an_attackerless_own_death_survives_the_lineup_filter(
    tmp_path: Path,
) -> None:
    """Pommiin kuollut oma pelaaja ei saa pudota suodattimessa.

    ``attacker_lineup_key`` on silloin ``null``, ja Polarsissa ``is_in``
    antaa nullille nullin. Ilman ``fill_null(False)``:ää ehto nojaisi siihen,
    että ``true | null`` on tosi -- oikein tänään, mutta hiljainen riippuvuus
    kolmiarvoisen logiikan yksityiskohdasta.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    only_bomb = deaths_frame(
        [
            death_row(
                "Ancient_vs_a",
                1,
                victim=f"{TEAM}-p0",
                victim_lineup=TEAM,
                victim_area="BombsiteB",
                attacker=None,
                t_s=95.0,
            )
        ]
    )
    only_bomb.write_parquet(archive.parsed_table("Ancient_vs_a", "deaths"))

    run(archive, force=True)
    entry = read_report(archive).maps[0].sides[0].round_types[0]
    assert entry.deaths.m == 1
    assert [(a.area, a.n) for a in entry.deaths.first_death_areas] == [
        ("BombsiteB", 1)
    ]
    assert entry.deaths.kills_total == 0


def test_a_deaths_table_that_names_no_known_lineup_is_refused(
    tmp_path: Path,
) -> None:
    """Tyhjäksi suodattunut kuolemakehys on sama tila, jonka parse kieltää.

    Ilman vartijaa jokainen kierrostyyppi raportoisi "ei omia kuolemia" --
    eli havaintona sen, ettei havaintoa ole.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    strangers = deaths_frame(
        [
            death_row(
                "Ancient_vs_a",
                1,
                victim="x1",
                victim_lineup="tuntematonkokoonp",
                attacker="x2",
                attacker_lineup="tuntematonkokoonp",
            )
        ]
    )
    strangers.write_parquet(archive.parsed_table("Ancient_vs_a", "deaths"))

    with pytest.raises(PappascoutError) as exc:
        run(archive, force=True)
    assert "yhtään kuolemaa" in str(exc.value)
    assert "--pakota" in str(exc.value)


# --- Kierrostaulu ja panssarilaskuri (Story 2.8) --------------------------------


def test_an_outdated_rounds_table_tells_the_user_to_reparse(
    tmp_path: Path,
) -> None:
    """Vanha kierrostaulu ilman panssarisaraketta ei mene läpi hiljaa.

    I/O-matriisin rivi "vanha arkisto" aggregoinnin puolelta: skeemavirhe on
    suomenkielinen ja nimeää sekä puuttuvan sarakkeen että komennon.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    path = archive.parsed_table("Ancient_vs_a", "rounds")
    pl.read_parquet(path).drop(ARMORED_COLUMN).write_parquet(path)

    with pytest.raises(SchemaError) as exc:
        run(archive)
    assert ARMORED_COLUMN in str(exc.value)
    assert "uv run pappascout parse Ancient_vs_a --pakota" in str(exc.value)


def test_an_extra_column_in_the_rounds_table_is_refused_too(
    tmp_path: Path,
) -> None:
    """Sopimus on tiukka molempiin suuntiin, myös kierrostaululla.

    Ylimääräinen sarake tarkoittaa taulua, jonka joku muu versio kirjoitti;
    pelkkä puuttuvan tarkistus päästäisi sen läpi.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    path = archive.parsed_table("Ancient_vs_a", "rounds")
    df = pl.read_parquet(path)
    df.with_columns(pl.lit(1).alias("ylimaarainen")).write_parquet(path)

    with pytest.raises(SchemaError) as exc:
        run(archive)
    assert "ylimaarainen" in str(exc.value)


def test_the_armored_count_reaches_the_report_from_the_rounds_table(
    tmp_path: Path,
) -> None:
    """Kytkentä levyltä raporttiin: laskuri ei ole luokitellussa taulussa.

    ``build_archive`` kirjoittaa omalle joukkueelle viisi kevlaria ja
    vastustajalle nolla. Ilman tätä testiä ``rounds``-taulun luku voisi
    puuttua vaiheesta kokonaan ja jakauma olisi hiljaa tyhjä.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})

    run(archive)
    entry = read_report(archive).maps[0].sides[0].round_types[0]
    assert [(c.armored, c.n) for c in entry.players_armored.counts] == [(5, 1)]
    assert entry.players_armored.rounds_unknown == 0


def test_only_our_own_rounds_row_reaches_the_distribution(tmp_path: Path) -> None:
    """Kierrostaulussa on kaksi riviä per kierros -- vain oma päätyy jakaumaan.

    Vastustajalla on nolla kevlaria samalla kierroksella. Kaksi estettä
    yhdessä: kokoonpanosuodatus pudottaa hänen rivinsä ennen hakukarttaa, ja
    avaimen kolmas osa (puoli) valitsisi oman rivin silloinkin, jos suodatus
    puuttuisi. Testi todentaa lopputuloksen, jonka molemmat takaavat.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})

    run(archive)
    for map_report in read_report(archive).maps:
        for side_report in map_report.sides:
            for entry in side_report.round_types:
                assert [c.armored for c in entry.players_armored.counts] == [5]


def test_rounds_that_name_no_known_lineup_are_refused(tmp_path: Path) -> None:
    """Tyhjäksi suodattunut kierrostaulu on virhe, ei hiljainen puute.

    Sama vartija kuin kuolemataululla ja samasta syystä: ilman sitä jokainen
    kierrostyyppi raportoisi panssarijakaumakseen pelkän "havainto puuttuu"
    -- eli havaintona sen, ettei havaintoa ole. Juuri sen lopputuloksen
    välttäminen on tämän sarakkeen olemassaolon syy.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    strangers = rounds_frame(
        [
            round_row("Ancient_vs_a", n, lineup="tuntematonkokoonp", side="T")
            for n in (1, 2)
        ]
        + [
            round_row("Ancient_vs_a", n, lineup=OPPONENT, side="CT")
            for n in (1, 2)
        ]
    )
    strangers.write_parquet(archive.parsed_table("Ancient_vs_a", "rounds"))

    with pytest.raises(PappascoutError) as exc:
        run(archive, force=True)
    assert "kierrosriviä" in str(exc.value)
    assert "--pakota" in str(exc.value)


# --- Kartan nimi otsikosta (Story 2.11) ---------------------------------------


def test_the_map_name_comes_from_the_match_table(tmp_path: Path) -> None:
    """Havainto voittaa päättelyn myös vaiheen läpi ajettuna.

    Tunniste sanoo ``Nuke``, otsikko sanoo ``de_ancient``. Raportissa on
    otsikon nimi ja lähde ``demo_header``: ilman kytkentää taulusta
    raporttiin tämä testi näyttäisi täsmälleen samalta kuin ennen muutosta.
    """
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM},
        map_names={"Nuke_vs_a": "de_ancient"},
    )

    run(archive)
    report = read_report(archive)

    assert [(m.map_name, m.map_name_source) for m in report.maps] == [
        ("de_ancient", "demo_header")
    ]


def test_two_faceit_demos_of_the_same_map_are_one_branch(tmp_path: Path) -> None:
    """RCAVE-tapaus: kaksi tunnistetta, yksi kartta, yksi haara.

    Kumpikaan tunniste ei sisällä kartan nimeä, joten ilman otsikkoa nämä
    olisivat kaksi haaraa -- ja jokainen rivi kantaisi merkintää
    "(1/1 kierroksesta)".
    """
    archive = build_archive(
        tmp_path,
        {"1-a52ebff2-1-1": TEAM, "1-79f71e00-1-1": TEAM},
        map_names={
            "1-a52ebff2-1-1": "de_ancient",
            "1-79f71e00-1-1": "de_ancient",
        },
    )

    run(archive)
    report = read_report(archive)

    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_ancient"
    assert sorted(entry.map_demo_ids) == ["1-79f71e00-1-1", "1-a52ebff2-1-1"]
    assert entry.sample.demos == 2


def test_a_demo_without_a_header_name_falls_back_to_the_identifier(
    tmp_path: Path,
) -> None:
    """Nimetön otsikko ei kaada ajoa: päättely poolista jää voimaan."""
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "1-a52ebff2-1-1": TEAM},
        map_names={"Nuke_vs_a": None, "1-a52ebff2-1-1": None},
    )

    result = run(archive)
    assert result.status == "ok"

    report = read_report(archive)
    branches = {m.map_name: m.map_name_source for m in report.maps}
    assert branches == {"de_nuke": "map_demo_id", "1-a52ebff2-1-1": "unknown"}


def test_a_match_table_written_with_a_different_column_order_still_reads(
    tmp_path: Path,
) -> None:
    """Sarakejärjestys ei ole osa sopimusta, mutta ``pl.concat`` välittää."""
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM},
        map_names={"Nuke_vs_a": "de_nuke", "Anubis_vs_b": "de_anubis"},
    )
    path = archive.parsed_table("Anubis_vs_b", "match")
    df = pl.read_parquet(path)
    df.select(reversed(df.columns)).write_parquet(path)

    run(archive)
    report = read_report(archive)
    assert sorted(m.map_name for m in report.maps) == ["de_anubis", "de_nuke"]


def test_an_observed_and_an_inferred_demo_of_one_map_are_one_branch(
    tmp_path: Path,
) -> None:
    """Sama kartta kahdesta eri lähteestä on yksi haara, myös vaiheen läpi.

    ``Ancient_vs_a``:n otsikossa on kartta, ``Ancient_vs_b``:n ei. Molempien
    nimi on ``de_ancient``, joten ne ovat yksi haara -- ja sen lähde on
    heikompi eli ``map_demo_id``, koska toinen jäsen on päätelty.
    """
    archive = build_archive(
        tmp_path,
        {"Ancient_vs_a": TEAM, "Ancient_vs_b": TEAM},
        map_names={"Ancient_vs_a": "de_ancient", "Ancient_vs_b": None},
    )

    run(archive)
    report = read_report(archive)

    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_ancient"
    assert entry.map_name_source == "map_demo_id"
    assert entry.sample.demos == 2
    assert sorted(entry.map_demo_ids) == ["Ancient_vs_a", "Ancient_vs_b"]


def test_the_map_name_follows_the_demo_the_table_was_read_for(
    tmp_path: Path,
) -> None:
    """Nimi liitetään **luettuun demoon**, ei taulun omaan sarakkeeseen.

    Vanhentunut tai väärään hakemistoon joutunut ``match.parquet`` kantaa
    väärää ``map_demo_id``-arvoa; skeemavalidointi ei näe sitä, koska sarake on
    tyypiltään oikea. Jos sanakirja rakennettaisiin sarakkeesta, nimi
    kirjautuisi väärälle demolle ja oikea demo palaisi hiljaa päättelyyn --
    ja kaksi samaa tunnistetta pudottaisi toisen kokonaan.

    Tässä molempien demojen taulu väittää olevansa saman kolmannen demon.
    Silmukan avaimella nimet päätyvät silti oikeille demoille.
    """
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM},
        map_names={"Nuke_vs_a": "de_nuke", "Anubis_vs_b": "de_anubis"},
    )
    for demo in ("Nuke_vs_a", "Anubis_vs_b"):
        path = archive.parsed_table(demo, "match")
        pl.read_parquet(path).with_columns(
            pl.lit("Vieras_vs_x").alias("map_demo_id")
        ).write_parquet(path)

    run(archive)
    report = read_report(archive)

    assert sorted(m.map_name for m in report.maps) == ["de_anubis", "de_nuke"]
    assert all(m.map_name_source == "demo_header" for m in report.maps)


@pytest.mark.parametrize("rows", [0, 2])
def test_a_match_table_without_exactly_one_row_is_refused(
    tmp_path: Path, rows: int
) -> None:
    """Rivimäärä tarkistetaan myös luettaessa, ei vain kirjoitettaessa.

    Sopimusta valvoo se vaihe, joka kirjoittaa -- mutta luettu tiedosto voi
    olla ohjelman vanhemman version kirjoittama, eikä lukija saa nojata
    siihen että kirjoittaja oli tämä versio. Nolla riviä näyttäisi samalta
    kuin havainto ``null``, ja kahdesta rivistä nimi valikoituisi
    rivijärjestyksen mukaan.
    """
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM}, map_names={"Nuke_vs_a": "de_nuke"}
    )
    path = archive.parsed_table("Nuke_vs_a", "match")
    df = pl.read_parquet(path)
    pl.concat([df] * rows if rows else [df.head(0)]).write_parquet(path)

    with pytest.raises(PappascoutError, match="ottelutaulussa on"):
        run(archive)


# --- Poikkeamien orientaatio (Story 2.5) ----------------------------------------
#
# Vaiheen oma osa poikkeamasäännöistä on **alueiden puoliorientaatio**, ja se
# lasketaan ennen kokoonpanosuodatusta. Testit todistavat sen kahdesta
# suunnasta: mitä orientaatio laskee, ja mitä katoaa jos suodatus siirretään
# sen eteen.

#: Alue, jota vastustaja pitää hallussaan koko demossa -- Nuken lobby.
LOBBY = "Lobby"


def lobby_ticks(demo: str) -> list[dict[str, object]]:
    """Näytepisteet, joissa subjekti puskee vastustajan alueelle.

    Vastustaja (T) on ``Lobby``ssa jokaisella näytepisteellä jokaisella
    kierroksella: 45 havaintoa. Subjektin CT-pelaajat ovat omalla puolellaan,
    ja **vain kierroksella 1** kaksi heistä siirtyy sinne kahdesta eri
    suunnasta -- 2 havaintoa.

    Luvut ovat valittu niin, että sama alue on kynnyksen **eri puolilla**
    riippuen siitä, mistä taulusta orientaatio lasketaan: koko taulusta
    T-osuus on 45/47 = 0,96, mutta subjektin riveistä 0/2. Juuri sitä eroa
    kalibrointi mittasi oikeilla demoilla.
    """
    rows: list[dict[str, object]] = []
    for round_no in (1, 2, 3):
        for seconds in (6.0, 15.0, 30.0):
            rows += [
                tick_row(
                    demo,
                    round_no,
                    f"{OPPONENT}-p{i}",
                    LOBBY,
                    lineup=OPPONENT,
                    side="T",
                    sample_t_s=seconds,
                )
                for i in range(5)
            ]
        rows += [
            tick_row(
                demo, round_no, f"{TEAM}-p{i}", "Outside", side="CT", sample_t_s=6.0
            )
            for i in range(5)
        ]
        # Kaksi eri lähtöaluetta kierroksella 1, jotta crunch on mahdollinen.
        rows += [
            tick_row(
                demo, round_no, f"{TEAM}-p0", "Ramp", side="CT", sample_t_s=15.0
            ),
            tick_row(
                demo, round_no, f"{TEAM}-p1", "Squeaky", side="CT", sample_t_s=15.0
            ),
        ]
        rows += [
            tick_row(
                demo, round_no, f"{TEAM}-p{i}", "Outside", side="CT", sample_t_s=15.0
            )
            for i in range(2, 5)
        ]
        rows += [
            tick_row(
                demo,
                round_no,
                f"{TEAM}-p{i}",
                LOBBY if round_no == 1 and i < 2 else "Outside",
                side="CT",
                sample_t_s=30.0,
            )
            for i in range(5)
        ]
    return rows


def eco_ct_rounds(demo: str, count: int = 3) -> list[dict[str, object]]:
    """Luokitellut kierrokset: subjekti CT:nä säästökierroksilla."""
    return [
        classified_row(demo, n, side="CT", round_type="eco")
        for n in range(1, count + 1)
    ]


def test_the_orientation_counts_t_rows_against_all_rows() -> None:
    """Osuus on demon oma havainto: T-havainnot per kaikki havainnot."""
    demo = "Nuke_vs_a"
    found = aggregate_stage._area_orientation(ticks_frame(lobby_ticks(demo)))
    assert found[LOBBY].t == 45
    assert found[LOBBY].total == 47
    assert found[LOBBY].t_share == pytest.approx(45 / 47)


def test_the_orientation_ignores_first_contact_dead_and_unnamed_rows() -> None:
    """Neljä rajausta, jokainen määritelmä eikä siivous."""
    demo = "Nuke_vs_a"
    rows = [
        tick_row(demo, 1, "p1", "Ramp", side="T"),
        tick_row(demo, 1, "p2", "Ramp", side="T", sample_kind="first_contact"),
        tick_row(demo, 1, "p3", "Ramp", side="T", is_alive=False),
        tick_row(demo, 1, "p4", None, side="T"),
        tick_row(demo, 1, "p5", "Ramp", side="T"),
    ]
    rows[-1]["side"] = None
    found = aggregate_stage._area_orientation(ticks_frame(rows))
    assert set(found) == {"Ramp"}
    assert found["Ramp"].total == 1


def test_an_unknown_side_is_left_out_of_the_denominator_too() -> None:
    """**Jakajan rajaus, ei osoittajan.**

    Ilman ``side``-suodatusta ``pl.len()`` laskisi tuntemattoman puolen rivin
    mukaan mutta ``side == "T"`` ei voisi laskea sitä T:ksi -- osuus painuisi
    alaspäin ja voisi pudottaa T:n alueen kynnyksen alle. Juuri se osuus on
    molempien sääntöjen perusta, joten vartija on tässä eikä säännössä.
    """
    demo = "Nuke_vs_a"
    rows = [tick_row(demo, 1, f"t{i}", "Lobby", side="T") for i in range(9)]
    rows.append(tick_row(demo, 1, "x", "Lobby", side="CT"))
    clean = aggregate_stage._area_orientation(ticks_frame(rows))
    assert clean["Lobby"].t_share == pytest.approx(0.9)

    with_null = [dict(row) for row in rows]
    for _ in range(4):
        extra = dict(rows[0])
        extra["player_id"] = f"tuntematon{_}"
        extra["side"] = None
        with_null.append(extra)
    dirty = aggregate_stage._area_orientation(ticks_frame(with_null))
    # Tuntemattoman puolen rivit eivät ole kummassakaan luvussa, joten osuus
    # on sama kuin ilman niitä.
    assert dirty["Lobby"].t_share == pytest.approx(0.9)
    assert dirty["Lobby"].total == clean["Lobby"].total


def test_the_orientation_normalises_the_area_name() -> None:
    """Sama normalisointi kuin läsnäolorivillä; muuten säännöt vaikenevat.

    ``" Lobby "`` ja ``"Lobby"`` ovat yksi alue, ja niiden havainnot
    **lasketaan yhteen**: toisen pudottaminen laskisi osuuden osajoukosta ja
    voisi kääntää kynnyksen.
    """
    demo = "Nuke_vs_a"
    rows = [
        tick_row(demo, 1, "t1", "Lobby", side="T"),
        tick_row(demo, 1, "t2", " Lobby ", side="T"),
        tick_row(demo, 1, "c1", "Lobby\t", side="CT"),
        tick_row(demo, 1, "c2", "   ", side="CT"),
    ]
    found = aggregate_stage._area_orientation(ticks_frame(rows))
    assert set(found) == {"Lobby"}
    assert found["Lobby"].t == 2
    assert found["Lobby"].total == 3


def test_a_demo_without_named_areas_gets_an_empty_orientation() -> None:
    """Tyhjä kartta on oikea vastaus eikä virhe: suuntaa ei arvata."""
    demo = "Nuke_vs_a"
    rows = [tick_row(demo, 1, "p1", None, side="T")]
    assert aggregate_stage._area_orientation(ticks_frame(rows)) == {}


def test_the_orientation_must_come_from_the_unfiltered_table() -> None:
    """**Suodatus siirrettynä orientaation eteen syö osuman kokonaan.**

    Tämä on Story 2.5:n mitattu ehto koneellisena vartijana. Sama taulu,
    sama sääntö, sama kynnys -- ainoa ero on se, lasketaanko orientaatio
    koko taulusta vai subjektin riveistä. Ilman tätä testiä ``_aggregate``in
    silmukan voisi siirtää suodatuksen jälkeen, ja jokainen tosi positiivinen
    katoaisi ilman että yksikään testi kaatuisi.
    """
    demo = "Nuke_vs_a"
    ticks = ticks_frame(lobby_ticks(demo))
    classified = eco_ct_rounds(demo)
    subject = ticks.filter(pl.col("lineup_key") == TEAM)

    from_all = aggregate_stage._area_orientation(ticks)
    from_subject = aggregate_stage._area_orientation(subject)
    assert from_all[LOBBY].t_share > 0.80
    assert from_subject[LOBBY].t_share == 0.0

    # ``build_report`` näkee molemmissa tapauksissa vain subjektin rivit;
    # vain orientaation lähde vaihtuu.
    subject_rows = subject.to_dicts()
    found = report_for(
        classified, subject_rows, area_orientation={demo: from_all}
    )
    lost = report_for(
        classified, subject_rows, area_orientation={demo: from_subject}
    )
    assert [a.rule for a in found.anomalies] == ["ct_advance", "crunch"]
    assert lost.anomalies == []


def test_the_stage_reads_the_orientation_before_it_filters(
    tmp_path: Path,
) -> None:
    """Koko vaihe päästä päähän: poikkeama on ``report.json``issa."""
    demo = "Nuke_vs_a"
    archive = build_archive(tmp_path, {demo: TEAM}, rounds=3)
    classified_frame(eco_ct_rounds(demo)).write_parquet(
        archive.classified(TEAM, demo)
    )
    ticks_frame(lobby_ticks(demo)).write_parquet(
        archive.parsed_table(demo, "ticks")
    )
    run(archive)
    report = read_report(archive)
    assert [a.rule for a in report.anomalies] == ["ct_advance", "crunch"]
    advance = report.anomalies[0]
    assert advance.map_name == "de_nuke"
    assert advance.map_name_source == "map_demo_id"
    assert advance.side == "CT"
    assert advance.round_types == ["eco"]
    assert advance.area == LOBBY
    assert (advance.n, advance.m) == (1, 3)
    assert advance.players_max == 2
    assert advance.rounds[0].round_no == 1
    assert advance.orientation[0].observations == 47
    crunch = report.anomalies[1]
    assert crunch.rounds[0].sources == ["Ramp", "Squeaky"]
    assert report.anomaly_scan.rounds_scanned == 3
    assert report.anomaly_scan.crunch_rounds == 3
    assert report.anomaly_scan.advance_rounds == 3
    assert report.anomaly_scan.demos_without_orientation == []


def test_the_run_output_names_the_anomalies_and_the_coverage(
    tmp_path: Path,
) -> None:
    """Ajon tuloste kertoo poikkeamista: muuten säätö näkyy vasta raportissa."""
    demo = "Nuke_vs_a"
    archive = build_archive(tmp_path, {demo: TEAM}, rounds=3)
    classified_frame(eco_ct_rounds(demo)).write_parquet(
        archive.classified(TEAM, demo)
    )
    ticks_frame(lobby_ticks(demo)).write_parquet(
        archive.parsed_table(demo, "ticks")
    )
    text = _render_aggregate(run(archive))
    assert "Poikkeamat" in text
    assert "ct_advance de_nuke CT eco: Lobby 2 pelaajaa (1/3)" in text
    assert (
        "säännöt ct_advance, crunch ajettiin 3 kierrokselle -- crunch voi "
        "osua 3 ja eteneminen 3"
    ) in text
    assert "ajamatta stack" in text


def test_the_run_output_says_when_a_demo_has_no_orientation(
    tmp_path: Path,
) -> None:
    """Sokea piste näkyy myös ajon tulosteessa, ei vain raportissa."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    text = _render_aggregate(run(archive))
    assert "ilman alueorientaatiota 1 demoa" in text


def test_a_demo_without_anomalies_writes_an_empty_list(tmp_path: Path) -> None:
    """Tyhjä poikkeamalista on kelvollinen tulos eikä puuttuva kenttä.

    Kattavuus kirjoitetaan silti: oletusarkiston näytepisteissä on vain kaksi
    aluetta ja kolme havaintoa, joten yksikään ei ylitä havaintokynnystä --
    eli tyhjä luku on **sokea piste** eikä mitattu negatiivinen, ja juuri se
    ero on kattavuuden tehtävä sanoa.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    report = read_report(archive)
    assert report.anomalies == []
    assert report.anomaly_scan.rules == ["ct_advance", "crunch"]
    assert report.anomaly_scan.rules_deferred == ["stack"]
    assert report.anomaly_scan.demos_without_orientation == ["Nuke_vs_a"]


#: Jokaiselle poikkeamakynnykselle **kelvollinen** muutos oletuksesta.
#:
#: Sanakirja eikä yksittäinen avain, koska ``crunch_min_sources`` ei voi
#: liikkua yksin: alaraja on 2 (crunch on määritelmällisesti useaa suuntaa) ja
#: validaattori kieltää suuntia enemmän kuin pelaajia, joten sen ainoa suunta
#: ylöspäin nostaa myös ``crunch_min_players``ia. Se sanotaan tässä ääneen,
#: jotta puuttuva avain ei näytä unohdukselta.
ANOMALY_THRESHOLD_CHANGES: tuple[tuple[str, dict[str, object]], ...] = (
    ("advance_t_share", {"advance_t_share": 0.9}),
    ("advance_area_min_observations", {"advance_area_min_observations": 40}),
    ("advance_max_sample_s", {"advance_max_sample_s": 15.0}),
    ("advance_min_players", {"advance_min_players": 2}),
    ("crunch_min_players", {"crunch_min_players": 3}),
    (
        "crunch_min_sources",
        {"crunch_min_players": 3, "crunch_min_sources": 3},
    ),
)


@pytest.mark.parametrize(
    "key,overrides",
    ANOMALY_THRESHOLD_CHANGES,
    ids=[name for name, _ in ANOMALY_THRESHOLD_CHANGES],
)
def test_every_anomaly_threshold_changes_the_params_hash(
    tmp_path: Path, key: str, overrides: dict[str, object]
) -> None:
    """Ilman tätä kynnyksen säätö ei ajaisi aggregointia uudelleen.

    Sama vika kuin Story 1.8:ssa: raportti pitäisi vanhat poikkeamat, ja
    käyttäjä näkisi säädön vaikutuksen vasta ``--pakota``lla.

    **Tämä on ajonaikainen vartija**, toisin kuin
    :func:`test_every_setting_the_stage_reads_is_in_the_params_hash`, joka
    lukee luetut kentät lähdetekstistä regexillä eikä aja hashia lainkaan.
    Kynnyksen vaikutus **raportin sisältöön** todistetaan erikseen
    ``test_aggregate.py``:n poikkeamalohkossa.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    before = Manifest.read(archive.report_manifest(TEAM)).params_hash
    aggregate_stage.run(
        thresholds(**overrides),
        _league(),
        archive,
        TEAM,
        aggregate_settings=aggregate_settings(),
    )
    assert Manifest.read(archive.report_manifest(TEAM)).params_hash != before


def test_every_hashed_anomaly_threshold_has_a_runtime_case() -> None:
    """Luettelo ei saa vanheta hiljaa.

    Hashattujen avainten ja tämän tiedoston ajonaikaisten tapausten on
    katettava samat poikkeamakynnykset. Ilman tätä uusi kynnys voisi päätyä
    hashiin ilman yhtäkään ajoa, joka todistaa sen vaikuttavan -- eli
    täsmälleen se tila, jonka katselmus löysi ``crunch_min_sources``ista.
    """
    hashed = {
        key
        for key in aggregate_stage.HASHED_THRESHOLD_KEYS
        if key.startswith(("advance_", "crunch_"))
    }
    covered = {name for name, _ in ANOMALY_THRESHOLD_CHANGES}
    assert hashed == covered
