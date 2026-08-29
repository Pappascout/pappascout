"""``stages.classify`` -- vaiheen testit.

Vaihe ei lue demoa lainkaan, joten sen koko logiikka -- kierrostaulun luku,
molempien joukkueiden luokittelu, kierroslista, manifesti ja ohitus --
testataan käsin rakennetulla kierrostaululla. Ainoat demoa vaativat testit ovat
lopun regressiot, ja ne ohittavat itsensä siististi.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from conftest import (
    ANCIENT_DEM,
    ANCIENT_ROUNDS,
    NUKE_ROUNDS,
    NUKE_ZST,
    REAL_SETTINGS,
    has_temp_leftovers,
    require_demo,
    settings_text,
)
from pappascout.adapters.protocols import (
    EVENTS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
)
from pappascout.archive.manifest import Manifest
from pappascout.archive.paths import ArchivePaths
from pappascout.domain.economy import per_player
from pappascout.domain.models import load_settings
from pappascout.domain.rounds import mark_played_rounds
from pappascout.domain.schemas import CLASSIFIED, EVENTS, ROUNDS, TICKS, validate
from pappascout.errors import PappascoutError, SchemaError
from pappascout.stages import classify as classify_stage
from pappascout.stages import parse as parse_stage

MAP_DEMO_ID = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1"

A = "aaaaaaaaaaaaaaaa"
B = "bbbbbbbbbbbbbbbb"


# --- Kierrostaulun rakennus ------------------------------------------------------


def round_rows(
    round_no: int,
    *,
    a_side: str = "T",
    a_won: bool = True,
    a_money: int = 5000,
    a_spent: int = 20000,
    a_equip: int = 25000,
    a_start: int = 1000,
    b_money: int = 5000,
    b_spent: int = 20000,
    b_equip: int = 25000,
    b_start: int = 1000,
    a_players: int | None = 5,
    b_players: int | None = 5,
    status: str = "ok",
) -> list[dict[str, object]]:
    """Yhden kierroksen kaksi riviä, yksi kummallekin kokoonpanolle.

    Voiton syy valitaan puolen mukaan, jotta CS2:n sääntöinvariantti pysyy
    voimassa myös tässä käsin rakennetussa taulussa.
    """
    b_side = "CT" if a_side == "T" else "T"
    win_reason = "ct_killed" if (a_side == "T") == a_won else "t_killed"
    rows = []
    for lineup, side, won, money, spent, equip, start, players in (
        (A, a_side, a_won, a_money, a_spent, a_equip, a_start, a_players),
        (B, b_side, not a_won, b_money, b_spent, b_equip, b_start, b_players),
    ):
        rows.append(
            {
                "map_demo_id": MAP_DEMO_ID,
                "round_raw": round_no + 1,
                "round_no": round_no,
                "lineup_key": lineup,
                "side": side,
                "won": won,
                "win_reason": win_reason,
                "money_freeze_end": None if status != "ok" else money,
                "money_spent": None if status != "ok" else spent,
                "equip_freeze_end": None if status != "ok" else equip,
                "equip_round_start": None if status != "ok" else start,
                "players_freeze_end": None if status != "ok" else players,
                "survivors": 2 if won else 0,
                "survivors_equip_prev": 0,
                "freeze_end_tick": None if status != "ok" else 1000 * round_no,
                "tick_rate": 64.0,
                "status": status,
            }
        )
    return rows


def rounds_frame(rounds: list[list[dict[str, object]]]) -> pl.DataFrame:
    rows = [r for pair in rounds for r in pair]
    df = pl.DataFrame(rows, schema=dict(ROUNDS), orient="row")
    return validate(df, ROUNDS, "rounds")


def match(played: int = 6) -> list[list[dict[str, object]]]:
    """Yksinkertainen ottelu: A voittaa pistoolin, sen jälkeen vuorotellen."""
    rounds = [round_rows(1, a_won=True, a_equip=4000, b_equip=4000)]
    for no in range(2, played + 1):
        rounds.append(round_rows(no, a_won=no % 2 == 0))
    return rounds


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    root = tmp_path / "arkisto"
    root.mkdir()
    return ArchivePaths(root=root)


@pytest.fixture
def settings(settings_file: Path):
    return load_settings(settings_file, env_files=())


def _minimal_ticks(frame: pl.DataFrame) -> pl.DataFrame:
    """Yksi näytepiste per kierrosrivi, portin sopimuksen mukaisena.

    Luokittelu ei lue näytepisteitä lainkaan, mutta ``parse`` kieltäytyy
    kirjoittamasta tyhjää asetelmataulua ei-tyhjälle kierrostaululle. Tämä
    pitää kiinnikkeen rehellisenä: se tuottaa sen mitä oikea adapteri
    tuottaisi, ei tyhjää kuorta.
    """
    rows = [
        {
            "round_raw": row["round_no"],
            "round_no": None,
            "player_id": f"{row['lineup_key']}-1",
            "lineup_key": row["lineup_key"],
            "side": row["side"],
            "sample_kind": "time",
            "sample_t_s": 6.0,
            "t_s": 6.0,
            "x": 1.0,
            "y": 2.0,
            "z": 3.0,
            "area": "Middle",
            "is_alive": True,
        }
        for row in frame.iter_rows(named=True)
        if row["round_no"] is not None
    ]
    return pl.DataFrame(
        rows,
        schema={name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS},
        orient="row",
    )


def write_parse(
    archive: ArchivePaths,
    frame: pl.DataFrame,
    parse_settings,
    *,
    force: bool = False,
) -> None:
    """Kirjoita kierrostaulu ja aito ``parse``-manifesti arkistoon.

    Manifesti kirjoitetaan oikealla vaiheella eikä käsin, jotta ohitusketju
    ``parse -> classify`` testataan sellaisena kuin se tuotannossa on.
    Demotiedostoa ei kirjoiteta uudelleen, jos se on jo olemassa: sen koko ja
    muokkausaika ovat osa parsinnan syötetunnistetta.
    """
    demo = archive.import_dir() / f"{MAP_DEMO_ID}.dem"
    demo.parent.mkdir(parents=True, exist_ok=True)
    if not demo.exists():
        demo.write_bytes(b"PBDEMS2\x00" + b"x" * 512)

    adapter = frame.drop("map_demo_id").with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no"),
        (pl.col("round_no") - 1).alias("score_start"),
        pl.col("round_no").alias("score_end"),
    )

    # Näytepistetaulu on Story 2.1:n tulos eikä vaikuta luokitteluun, mutta se
    # ei saa olla tyhjä: parse hylkää asetelmattoman tuloksen. Feikki antaa
    # siksi yhden näytepisteen per kierros, samoilla avaimilla kuin
    # kierrostaulussa.
    ticks_frame = _minimal_ticks(frame)

    # Utility ei vaikuta luokitteluun lainkaan, ja tyhjä tapahtumataulu on
    # kelvollinen tulos -- toisin kuin tyhjä asetelmataulu. Kiinnike antaa siis
    # tyhjän mutta sopimuksen mukaisen taulun.
    events_frame = pl.DataFrame(
        schema={name: EVENTS[name] for name in EVENTS_ADAPTER_COLUMNS}
    )

    class Fake:
        def parse_demo(self, path: Path, sample_seconds) -> DemoTables:
            return DemoTables(rounds=adapter, ticks=ticks_frame, events=events_frame)

    parse_stage.run(
        parse_settings, archive, MAP_DEMO_ID, Fake(), demo_path=demo, force=force
    )


@pytest.fixture
def parsed(archive: ArchivePaths, settings) -> ArchivePaths:
    write_parse(archive, rounds_frame(match()), settings.parse)
    return archive


def run_classify(settings, archive, team=A, **kwargs):
    return classify_stage.run(
        settings.thresholds, settings.league, archive, MAP_DEMO_ID, team, **kwargs
    )


# --- Onnistunut ajo --------------------------------------------------------------


def test_writes_a_valid_classified_table(settings, parsed) -> None:
    result = run_classify(settings, parsed)

    path = parsed.classified(A, MAP_DEMO_ID)
    assert path.is_file()
    df = pl.read_parquet(path)
    assert df.schema == dict(CLASSIFIED)
    assert df.height == 6, "yksi rivi per kierros, ei kahta"
    assert df["round_no"].to_list() == [1, 2, 3, 4, 5, 6]
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    assert result.status == "ok"
    assert not result.skipped
    assert result.stats["rounds"] == 6


def test_result_is_written_under_the_subject_team(settings, parsed) -> None:
    run_classify(settings, parsed, team=A)
    assert parsed.classified(A, MAP_DEMO_ID).is_file()
    assert not parsed.classified(B, MAP_DEMO_ID).exists()


def test_every_row_carries_a_reason_and_its_inputs(settings, parsed) -> None:
    """Ilman perustelua ja lähtöarvoja kalibrointi Story 1.4:ssä on mahdotonta."""
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    assert df["reason"].null_count() == 0
    assert all(len(r) > 20 for r in df["reason"].to_list())
    for inputs in df["inputs"].to_list():
        assert inputs["players"] == 5
        assert inputs["full_equip_min"] == settings.thresholds.full_equip_min


def test_pistol_round_is_classified_from_the_round_number(settings, parsed) -> None:
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    assert df.filter(pl.col("round_no") == 1)["round_type"].to_list() == ["pistol"]


def test_both_teams_are_classified_in_the_same_run(settings, parsed) -> None:
    """``opp_round_type`` on toisen joukkueen oma ``round_type`` samalta ajolta."""
    run_classify(settings, parsed, team=A)
    run_classify(settings, parsed, team=B)

    a = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).sort("round_no")
    b = pl.read_parquet(parsed.classified(B, MAP_DEMO_ID)).sort("round_no")

    assert a["round_type"].to_list() == b["opp_round_type"].to_list()
    assert b["round_type"].to_list() == a["opp_round_type"].to_list()
    assert a["side"].to_list() != b["side"].to_list()


def test_loss_count_is_written_per_round(settings, parsed) -> None:
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).sort("round_no")
    assert df["loss_count"][0] == settings.thresholds.loss_count_half_start
    assert df["loss_count"].is_between(
        settings.thresholds.loss_count_min, settings.thresholds.loss_count_max
    ).all()


def test_league_and_roster_fields_stay_empty_until_epic_three(
    settings, parsed
) -> None:
    """Arvaus olisi tässä pahempi kuin tyhjä: tieto tulee joukkueindeksistä."""
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    assert df["is_league"].null_count() == df.height
    assert df["roster_class"].null_count() == df.height


def test_write_is_atomic(settings, parsed) -> None:
    run_classify(settings, parsed)
    assert not has_temp_leftovers(parsed.root)


def test_nothing_is_written_into_the_parsed_area(settings, parsed) -> None:
    """``classify`` ei kirjoita toisen vaiheen tulosalueelle."""
    before = {
        p: p.stat().st_mtime_ns
        for p in (parsed.root / "parsed").rglob("*")
        if p.is_file()
    }
    run_classify(settings, parsed)
    after = {
        p: p.stat().st_mtime_ns
        for p in (parsed.root / "parsed").rglob("*")
        if p.is_file()
    }
    assert before == after


# --- Kierroslista Markdownina ----------------------------------------------------


def test_writes_a_readable_round_list_beside_the_table(settings, parsed) -> None:
    result = run_classify(settings, parsed)
    path = parsed.classified_round_list(A, MAP_DEMO_ID)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")

    assert MAP_DEMO_ID in text
    assert text.count("\n|") >= 6, "rivi jokaiselle kierrokselle"
    # Kynnykset ovat mukana, muuten lista ei kerro mitä vasten päätös tehtiin.
    # Tarkistus kohdistuu otsikon LAUSEESEEN, ei pelkkään lukuun: fikstuurin
    # rahasummat sisältävät samoja numeroita, joten irrallinen "1000" löytyisi
    # taulukon riveiltä vaikka otsikko olisi rikki.
    t = settings.thresholds
    header_line = next(r for r in text.splitlines() if r.startswith("- Kynnykset"))
    assert f"täysi osto vähintään {t.full_equip_min}" in header_line
    assert f"voiton jälkeen enintään {t.anomaly_equip_max_after_win}" in header_line
    assert f"ostettua vähintään {t.force_buy_min}" in header_line
    assert f"taskuun jäi enintään {t.force_money_left_max}" in header_line
    assert str(settings.league.ot_start_money) in text
    # Poistuneita kynnyksiä ei mainita: otsikko kertoo vain sen, mitä
    # luokittelu oikeasti vertaili.
    for retired in (
        "eco_money_max",
        "eco_loss_count_min",
        "force_money_min",
        "force_money_max",
        "half_equip_min",
    ):
        assert retired not in text
    assert str(path.relative_to(parsed.root)).replace("\\", "/") in [
        str(o) for o in result.outputs
    ]


def test_round_list_is_listed_as_an_output_in_the_manifest(settings, parsed) -> None:
    run_classify(settings, parsed)
    manifest = Manifest.read(parsed.classified_manifest(A, MAP_DEMO_ID))
    assert any(o.endswith(".md") for o in manifest.outputs)
    assert any(o.endswith(".parquet") for o in manifest.outputs)


# --- Manifesti ja ohitus ---------------------------------------------------------


def test_manifest_has_no_tool_versions(settings, parsed) -> None:
    """Luokittelu on puhdasta domain-laskentaa: mikään kirjastoversio ei muuta sitä."""
    run_classify(settings, parsed)
    manifest = Manifest.read(parsed.classified_manifest(A, MAP_DEMO_ID))
    assert manifest.stage == "classify"
    assert manifest.tool_versions == {}
    assert manifest.inputs[0].result_id == f"parsed/{MAP_DEMO_ID}"


def test_second_run_is_skipped(settings, parsed) -> None:
    run_classify(settings, parsed)
    path = parsed.classified(A, MAP_DEMO_ID)
    before = path.stat().st_mtime_ns

    result = run_classify(settings, parsed)
    assert result.skipped
    assert path.stat().st_mtime_ns == before
    assert result.stats["rounds"] == 6
    assert result.stats["rows"], "kierroslista luetaan valmiista tuloksesta"


def test_force_overrides_a_matching_manifest(settings, parsed) -> None:
    run_classify(settings, parsed)
    assert not run_classify(settings, parsed, force=True).skipped


def test_a_stale_inputs_struct_is_recomputed_not_read(settings, parsed) -> None:
    """Vanha tulos, jonka ``inputs``-rakenne on eri muotoa, ajetaan uudelleen.

    ``inputs``-structin kentät muuttuivat kalibroinnissa 2026-08-29 ilman että
    manifestin skeemaversio muuttui, joten täsmäävä manifesti voi osoittaa
    vanhamuotoiseen tauluun. Sen on johdettava uudelleenlaskentaan -- ei
    kaatumiseen eikä hiljaiseen vanhan tuloksen palauttamiseen.
    """
    run_classify(settings, parsed)
    path = parsed.classified(A, MAP_DEMO_ID)
    assert run_classify(settings, parsed).skipped, "esiehto: manifesti täsmää"

    # Kirjoita taulu uudelleen vanhanmallisella inputs-rakenteella: poistetut
    # kynnykset takaisin, uudet pois.
    df = pl.read_parquet(path)
    old_inputs = []
    for i in df["inputs"].to_list():
        retired_keys = ("force_buy_min", "force_money_left_max")
        row = {k: v for k, v in i.items() if k not in retired_keys}
        row["eco_money_max"] = 2000
        row["force_money_min"] = 1500
        old_inputs.append(row)
    df.with_columns(pl.Series("inputs", old_inputs)).write_parquet(path)

    result = run_classify(settings, parsed)
    assert not result.skipped, "vanhamuotoista tulosta ei saa palauttaa sellaisenaan"
    assert result.status == "ok"
    fields = set(pl.read_parquet(path)["inputs"].to_list()[0])
    assert "force_buy_min" in fields
    assert "eco_money_max" not in fields


def test_threshold_change_reruns_classify_but_not_parse(
    tmp_path: Path, archive
) -> None:
    """Hyväksymiskriteeri: kynnysmuutos ajaa luokittelun, ei parsintaa."""
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root, **{"full_equip_min = 4000": "full_equip_min = 4100"}
        ),
        encoding="utf-8",
    )
    a = load_settings(base_toml, env_files=())
    b = load_settings(changed_toml, env_files=())

    write_parse(archive, rounds_frame(match()), a.parse)
    parse_mtime_before = archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns

    run_classify(a, archive)
    result = run_classify(b, archive)

    assert not result.skipped, "kynnysmuutoksen jälkeen luokittelu ajetaan uudelleen"
    assert archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns == (
        parse_mtime_before
    ), "parsintaa ei saa ajaa uudelleen"


def test_a_forced_reparse_with_the_same_result_does_not_rerun_classify(
    settings, parsed
) -> None:
    """Luokittelun syöte on parsinnan **tulos**, ei sen ajohetki.

    Ilman tätä jokainen ``parse --pakota`` pakottaisi myös uuden luokittelun,
    vaikka kierrostaulu olisi tavu tavulta sama.
    """
    run_classify(settings, parsed)
    write_parse(parsed, rounds_frame(match()), settings.parse, force=True)
    assert run_classify(settings, parsed).skipped


def test_a_changed_demo_forces_a_new_classification(settings, parsed) -> None:
    """Uusi parsinta uudesta demosta ei saa jäädä vanhan luokittelun taakse."""
    run_classify(settings, parsed)
    demo = parsed.import_dir() / f"{MAP_DEMO_ID}.dem"
    demo.write_bytes(b"PBDEMS2\x00" + b"y" * 4096)
    write_parse(parsed, rounds_frame(match()), settings.parse)

    assert not run_classify(settings, parsed).skipped


def test_missing_output_forces_a_rerun(settings, parsed) -> None:
    run_classify(settings, parsed)
    parsed.classified(A, MAP_DEMO_ID).unlink()
    assert not run_classify(settings, parsed).skipped


def test_unreadable_result_is_recomputed_not_reported(settings, parsed) -> None:
    """Luokittelu on halpaa: rikkinäinen tulos lasketaan uudelleen."""
    run_classify(settings, parsed)
    parsed.classified(A, MAP_DEMO_ID).write_bytes(b"ei parquetia")

    result = run_classify(settings, parsed)
    assert not result.skipped
    assert result.stats["rounds"] == 6
    assert pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).height == 6


def test_result_that_no_longer_matches_the_contract_is_recomputed(
    settings, parsed
) -> None:
    """Täsmäävä manifesti ei riitä, jos tulostaulun sopimus on muuttunut.

    Skeeman laajentuminen ei muuta manifestin sisältöä, joten vanha tulos
    näyttäisi ajantasaiselta mutta siitä puuttuisivat uudet arvot.
    """
    run_classify(settings, parsed)
    path = parsed.classified(A, MAP_DEMO_ID)
    pl.read_parquet(path).drop("loss_count").write_parquet(path)

    result = run_classify(settings, parsed)
    assert not result.skipped
    assert "loss_count" in pl.read_parquet(path).columns


# --- Joukkueen valinta -----------------------------------------------------------


def test_team_can_be_given_as_a_unique_prefix(settings, parsed) -> None:
    run_classify(settings, parsed, team=A[:6])
    assert parsed.classified(A, MAP_DEMO_ID).is_file()


def test_unknown_team_lists_both_lineups_of_the_demo(settings, parsed) -> None:
    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed, team="eitallaista")
    message = str(exc.value)
    assert A in message
    assert B in message
    assert "ei täsmää" in message


def test_missing_team_lists_both_lineups_too(settings, parsed) -> None:
    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed, team=None)
    message = str(exc.value)
    assert A in message and B in message
    assert "--team" in message


def test_ambiguous_prefix_is_refused(settings, archive) -> None:
    """Yhteinen alkuosa ei saa valita kokoonpanoa arpomalla."""
    frame = rounds_frame(match()).with_columns(
        pl.when(pl.col("lineup_key") == A)
        .then(pl.lit("yhteinen1"))
        .otherwise(pl.lit("yhteinen2"))
        .alias("lineup_key")
    )
    write_parse(archive, frame, settings.parse)
    with pytest.raises(PappascoutError, match="useampaan"):
        run_classify(settings, archive, team="yhteinen")


# --- Virheet ---------------------------------------------------------------------


def test_unparsed_demo_tells_which_command_to_run(settings, archive) -> None:
    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, archive)
    message = str(exc.value)
    assert "ei ole vielä parsittu" in message
    assert "pappascout parse" in message


def test_failed_parse_is_not_classified_over(settings, parsed) -> None:
    manifest = Manifest.read(parsed.parsed_manifest(MAP_DEMO_ID))
    broken = manifest.model_copy(
        update={"status": "parse_failed", "reason": "demo katkennut"}
    )
    broken.write(parsed.parsed_manifest(MAP_DEMO_ID))

    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed)
    assert "parse_failed" in str(exc.value)
    assert "demo katkennut" in str(exc.value)


def test_missing_parse_manifest_is_a_finnish_error(settings, parsed) -> None:
    parsed.parsed_manifest(MAP_DEMO_ID).unlink()
    with pytest.raises(PappascoutError, match="manifestia ei löytynyt"):
        run_classify(settings, parsed)


def test_rounds_table_that_breaks_the_contract_is_refused(
    settings, parsed
) -> None:
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).drop("survivors").write_parquet(path)
    with pytest.raises(SchemaError, match="survivors"):
        run_classify(settings, parsed)


def test_a_round_without_an_anchor_does_not_break_the_run(
    settings, archive
) -> None:
    """I/O-matriisi: ankkuriton kierros jää luokittelematta, ajo jatkuu."""
    rounds = match()
    rounds[2] = round_rows(3, status="no_freeze_end")
    write_parse(archive, rounds_frame(rounds), settings.parse)

    result = run_classify(settings, archive)
    df = pl.read_parquet(archive.classified(A, MAP_DEMO_ID)).sort("round_no")

    assert df.height == 6
    assert df["round_type"][2] is None
    assert "no_freeze_end" in df["reason"][2]
    assert df["round_type"].null_count() == 1
    assert result.stats["unclassified"] == 1


def test_short_handed_team_is_divided_by_the_observed_count(
    settings, archive
) -> None:
    """Vajaa joukkue: per pelaaja -arvo lasketaan oikealla määrällä."""
    total = 4 * settings.thresholds.full_equip_min
    rounds = match()
    rounds[3] = round_rows(4, a_won=False, a_equip=total, a_players=4)
    write_parse(archive, rounds_frame(rounds), settings.parse)

    run_classify(settings, archive)
    df = pl.read_parquet(archive.classified(A, MAP_DEMO_ID)).sort("round_no")
    row = df.row(3, named=True)
    assert row["inputs"]["players"] == 4
    assert row["round_type"] == "full"




# --- Katselmuksen nostamat reunatapaukset ----------------------------------------


def test_unnumbered_rounds_are_dropped_and_counted(settings, archive) -> None:
    """Numeroimaton rivi kaataisi loss countin; se pudotetaan ja kerrotaan.

    Kierrostaulu kirjoitetaan tässä suoraan, koska ``parse`` ei itse päästä
    numeroimatonta riviä läpi -- mutta arkistossa voi olla vanhemmalla
    versiolla kirjoitettu taulu, eikä luokittelu saa kaatua siihen.
    """
    write_parse(archive, rounds_frame(match()), settings.parse)
    path = archive.parsed_table(MAP_DEMO_ID, "rounds")
    table = pl.read_parquet(path)
    unnumbered = table.head(2).with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no"),
        pl.lit(99, dtype=pl.Int32).alias("round_raw"),
    )
    pl.concat([unnumbered, table]).write_parquet(path)

    result = run_classify(settings, archive)
    assert result.stats["unnumbered"] == 1
    assert result.stats["rounds"] == 6
    df = pl.read_parquet(archive.classified(A, MAP_DEMO_ID))
    assert df["round_no"].null_count() == 0


def test_skipped_run_gives_exactly_the_same_round_list(settings, parsed) -> None:
    """Yksi polku kierroslistalle: ohitus ei saa näyttää eri lukuja.

    Jos tuore ja ohitettu ajo rakentaisivat rivit eri tavalla, ``--show``
    näyttäisi toisella ajolla esimerkiksi vastustajan talouden subjektin
    kierroksilla -- eikä mikään kertoisi siitä.
    """
    fresh = run_classify(settings, parsed)
    skipped_run = run_classify(settings, parsed)

    assert skipped_run.skipped
    assert not fresh.skipped
    assert skipped_run.stats["rows"] == fresh.stats["rows"]
    assert skipped_run.stats["by_type"] == fresh.stats["by_type"]


def test_league_change_reruns_classify_but_not_parse(tmp_path: Path, archive) -> None:
    """``[league]`` on osa luokittelun parametrihashia siinä missä kynnyksetkin."""
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root, **{"ot_start_money = 12500": "ot_start_money = 10000"}
        ),
        encoding="utf-8",
    )
    a = load_settings(base_toml, env_files=())
    b = load_settings(changed_toml, env_files=())

    write_parse(archive, rounds_frame(match()), a.parse)
    parse_mtime_before = archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns

    run_classify(a, archive)
    result = run_classify(b, archive)

    assert not result.skipped
    assert archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns == (
        parse_mtime_before
    )


def test_markdown_row_matches_the_parquet_row(settings, parsed) -> None:
    """Taulukon sisältö, ei vain sen muoto: tyyppi ja perustelu ovat samat."""
    run_classify(settings, parsed)
    text = parsed.classified_round_list(A, MAP_DEMO_ID).read_text(encoding="utf-8")
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).sort("round_no")
    expected = df.row(0, named=True)

    row = next(r for r in text.splitlines() if r.startswith("| 1 |"))
    cells = [s.strip() for s in row.strip("|").split("|")]
    headers = [o for o, _ in classify_stage.ROUND_LIST_COLUMNS]
    fields = dict(zip(headers, cells))

    assert fields["Tyyppi"] == str(expected["round_type"])
    assert fields["Vast."] == str(expected["opp_round_type"])
    assert fields["Loss"] == str(expected["loss_count"])
    assert fields["Puoli"] == str(expected["side"])
    # Perustelu on sama teksti, vain Markdown-suojaukset poistettuna.
    assert fields["Perustelu"].replace("\\", "") == str(expected["reason"]).replace(
        "\\", ""
    )
    # Ja per pelaaja -arvot vastaavat inputs-rakennetta.
    inputs = expected["inputs"]
    assert fields["Varusteet"] == str(
        per_player(inputs["equip_freeze_end"], inputs["players"])
    )


def test_markdown_escapes_everything_that_would_break_the_table(settings) -> None:
    """Rivinvaihto rikkoisi taulukon ja backtick söisi loput rivistä."""
    rows = [
        {
            "round_no": 1,
            "side": "T",
            "won": True,
            "round_type": "eco",
            "opp_round_type": "full",
            "loss_count": 1,
            "money_per_player": 100,
            "money_available_per_player": 200,
            "spent_per_player": 100,
            "equip_per_player": 300,
            "players": 5,
            "reason": "Rivi\nvaihto | putki `backtick`.",
        }
    ]
    text = classify_stage.render_round_list_markdown(
        rows,
        map_demo_id=MAP_DEMO_ID,
        team_key=A,
        thresholds=settings.thresholds,
        league=settings.league,
    )
    table_lines = [r for r in text.splitlines() if r.startswith("| 1 |")]
    assert len(table_lines) == 1, "rivinvaihto ei saa katkaista solua"
    row = table_lines[0]
    assert row.count("|") == len(classify_stage.ROUND_LIST_COLUMNS) + 1 + 1
    assert "\\`" in row
    assert "\\|" in row


def test_markdown_is_byte_identical_on_a_rerun(settings, parsed) -> None:
    """Ajohetki kuuluu manifestiin, ei tulosteeseen -- muuten erot eivät näy."""
    run_classify(settings, parsed)
    before = parsed.classified_round_list(A, MAP_DEMO_ID).read_bytes()
    run_classify(settings, parsed, force=True)
    assert parsed.classified_round_list(A, MAP_DEMO_ID).read_bytes() == before
    # Aikaleima on kuitenkin tallessa.
    assert Manifest.read(parsed.classified_manifest(A, MAP_DEMO_ID)).created_at


def test_rounds_table_of_another_demo_is_refused(settings, parsed) -> None:
    """Väärä parquet oikeassa polussa luokiteltaisiin väärän tunnisteen alle."""
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).with_columns(
        pl.lit("1-toinen-demo-1").alias("map_demo_id")
    ).write_parquet(path)

    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed)
    assert "toisen demon rivejä" in str(exc.value)
    assert "1-toinen-demo-1" in str(exc.value)


def test_three_lineups_are_refused_with_the_right_count(settings, archive) -> None:
    rounds = match(4)
    rounds[3][1]["lineup_key"] = "cccccccccccccccc"
    write_parse(archive, rounds_frame(rounds), settings.parse)

    with pytest.raises(SchemaError) as exc:
        run_classify(settings, archive)
    message = str(exc.value)
    assert "3 kokoonpanoa" in message
    assert "cccccccccccccccc" in message


def test_round_number_mismatch_between_teams_is_refused(settings, parsed) -> None:
    """Ilman tarkistusta vastustajan tyyppi liittyisi väärälle riville."""
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).with_columns(
        pl.when((pl.col("lineup_key") == B) & (pl.col("round_no") == 6))
        .then(pl.lit(7, dtype=pl.Int32))
        .otherwise(pl.col("round_no"))
        .alias("round_no")
    ).write_parquet(path)

    with pytest.raises(SchemaError, match="eivät täsmää"):
        run_classify(settings, parsed)


def test_team_keys_lists_both_lineups(settings, parsed) -> None:
    assert classify_stage.team_keys(parsed, MAP_DEMO_ID) == sorted([A, B])


def test_inputs_carry_the_money_that_was_available(settings, parsed) -> None:
    """Story 1.4 tarvitsee käytettävissä olleen rahan, ei vain jäljelle jäänyttä."""
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    for inputs in df["inputs"].to_list():
        assert inputs["money_spent"] == 20000
        assert inputs["money_freeze_end"] + inputs["money_spent"] == 25000
        assert inputs["force_buy_min"] == settings.thresholds.force_buy_min
        assert (
            inputs["force_money_left_max"]
            == settings.thresholds.force_money_left_max
        )


# --- Oikeat demot ----------------------------------------------------------------


def real_rounds(demo_name: str, map_demo_id: str) -> pl.DataFrame:
    """Oikean demon kierrostaulu ``ROUNDS``-muodossa, ilman arkistoa."""
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    # Yksi näytepiste riittää: tämä apuri käyttää vain kierrostaulua, ja
    # portti palauttaa molemmat samasta lukukerrasta. Poissulkulista on
    # tuotannon, jotta adapteri ajetaan samoilla säännöillä kuin oikeasti.
    parse_settings_real = load_settings(REAL_SETTINGS, env_files=()).parse
    adapter = Demoparser2Adapter(
        exclude_weapons=parse_settings_real.first_contact_exclude_weapons,
        fallback_death=parse_settings_real.first_contact_fallback_death,
    )
    tables = adapter.parse_demo(require_demo(demo_name), (6.0,))
    raw = mark_played_rounds(tables.rounds)
    df = raw.filter(pl.col("round_no").is_not_null()).select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in ROUNDS if name != "map_demo_id"],
    )
    return validate(df.sort("round_no", "side"), ROUNDS, "rounds")


def subject_key(df: pl.DataFrame) -> str:
    """Kokoonpano, joka aloitti T-puolella.

    Molemmissa testidemoissa se on ``team_SSStttNNN``
    (``_bmad-output/implementation-artifacts/testiaineisto.md``). Nimeä ei voi
    lukea demosta -- kierrostaulussa on vain kokoonpanotiiviste -- joten
    subjekti tunnistetaan aloituspuolesta.
    """
    return str(
        df.filter((pl.col("round_no") == 1) & (pl.col("side") == "T"))["lineup_key"][0]
    )


@pytest.mark.demo
def test_ancient_first_three_rounds_are_pistol_eco_full(settings_file: Path) -> None:
    """Regressio: todennettu jakso pistooli -> säästö -> täysi osto."""
    thresholds = load_settings(settings_file, env_files=()).thresholds
    df = real_rounds(ANCIENT_DEM, "ancient")
    df_, rows = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "ancient"
    )
    assert df_.height == ANCIENT_ROUNDS
    assert df_.sort("round_no")["round_type"].to_list()[:3] == ["pistol", "eco", "full"]
    # Perustelu kertoo rahan ja loss countin jokaisella kierroksella.
    assert all("loss count" in str(r["reason"]) for r in rows)


@pytest.mark.demo
def test_ancient_has_no_unclassified_rounds(settings_file: Path) -> None:
    thresholds = load_settings(settings_file, env_files=()).thresholds
    df = real_rounds(ANCIENT_DEM, "ancient")
    result, _ = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "ancient"
    )
    assert result["round_type"].null_count() == 0


@pytest.mark.demo
def test_nuke_overtime_rounds_get_no_economy_reasoning(settings_file: Path) -> None:
    thresholds = load_settings(settings_file, env_files=()).thresholds
    df = real_rounds(NUKE_ZST, "nuke")
    result, _ = classify_stage.classify_rounds(df, subject_key(df), thresholds, "nuke")

    assert result.height == NUKE_ROUNDS
    overtime = result.filter(pl.col("round_no") > thresholds.regulation_rounds)
    assert sorted(overtime["round_no"].to_list()) == [25, 26, 27, 28]
    assert set(overtime["round_type"].to_list()) == {"ot"}
    assert set(overtime["opp_round_type"].to_list()) == {"ot"}
    assert all("jatkoaikaa" in r for r in overtime["reason"].to_list())


@pytest.mark.demo
def test_nuke_first_three_rounds_are_pistol_eco_full(settings_file: Path) -> None:
    thresholds = load_settings(settings_file, env_files=()).thresholds
    df = real_rounds(NUKE_ZST, "nuke")
    result, _ = classify_stage.classify_rounds(df, subject_key(df), thresholds, "nuke")
    assert result.sort("round_no")["round_type"].to_list()[:3] == [
        "pistol",
        "eco",
        "full",
    ]


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_name,identifier", [(ANCIENT_DEM, "ancient"), (NUKE_ZST, "nuke")]
)
def test_opponent_type_matches_the_other_teams_own_type(
    settings_file: Path, demo_name: str, identifier: str
) -> None:
    thresholds = load_settings(settings_file, env_files=()).thresholds
    df = real_rounds(demo_name, identifier)
    a = subject_key(df)
    b = next(k for k in df["lineup_key"].unique().to_list() if k != a)

    own, _ = classify_stage.classify_rounds(df, a, thresholds, identifier)
    other, _ = classify_stage.classify_rounds(df, b, thresholds, identifier)

    assert own.sort("round_no")["round_type"].to_list() == (
        other.sort("round_no")["opp_round_type"].to_list()
    )
