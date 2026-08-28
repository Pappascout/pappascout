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


def kierros(
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
    syy = "ct_killed" if (a_side == "T") == a_won else "t_killed"
    rivit = []
    for lineup, side, won, money, spent, equip, start, players in (
        (A, a_side, a_won, a_money, a_spent, a_equip, a_start, a_players),
        (B, b_side, not a_won, b_money, b_spent, b_equip, b_start, b_players),
    ):
        rivit.append(
            {
                "map_demo_id": MAP_DEMO_ID,
                "round_raw": round_no + 1,
                "round_no": round_no,
                "lineup_key": lineup,
                "side": side,
                "won": won,
                "win_reason": syy,
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
    return rivit


def rounds_frame(kierrokset: list[list[dict[str, object]]]) -> pl.DataFrame:
    rivit = [r for kaksikko in kierrokset for r in kaksikko]
    df = pl.DataFrame(rivit, schema=dict(ROUNDS), orient="row")
    return validate(df, ROUNDS, "rounds")


def ottelu(pelatut: int = 6) -> list[list[dict[str, object]]]:
    """Yksinkertainen ottelu: A voittaa pistoolin, sen jälkeen vuorotellen."""
    kierrokset = [kierros(1, a_won=True, a_equip=4000, b_equip=4000)]
    for no in range(2, pelatut + 1):
        kierrokset.append(kierros(no, a_won=no % 2 == 0))
    return kierrokset


@pytest.fixture
def arkisto(tmp_path: Path) -> ArchivePaths:
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
    rivit = [
        {
            "round_raw": rivi["round_no"],
            "round_no": None,
            "player_id": f"{rivi['lineup_key']}-1",
            "lineup_key": rivi["lineup_key"],
            "side": rivi["side"],
            "sample_kind": "time",
            "sample_t_s": 6.0,
            "t_s": 6.0,
            "x": 1.0,
            "y": 2.0,
            "z": 3.0,
            "area": "Middle",
            "is_alive": True,
        }
        for rivi in frame.iter_rows(named=True)
        if rivi["round_no"] is not None
    ]
    return pl.DataFrame(
        rivit,
        schema={name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS},
        orient="row",
    )


def parsi(
    arkisto: ArchivePaths,
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
    demo = arkisto.import_dir() / f"{MAP_DEMO_ID}.dem"
    demo.parent.mkdir(parents=True, exist_ok=True)
    if not demo.exists():
        demo.write_bytes(b"PBDEMS2\x00" + b"x" * 512)

    adapteri = frame.drop("map_demo_id").with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no"),
        (pl.col("round_no") - 1).alias("score_start"),
        pl.col("round_no").alias("score_end"),
    )

    # Näytepistetaulu on Story 2.1:n tulos eikä vaikuta luokitteluun, mutta se
    # ei saa olla tyhjä: parse hylkää asetelmattoman tuloksen. Feikki antaa
    # siksi yhden näytepisteen per kierros, samoilla avaimilla kuin
    # kierrostaulussa.
    tickit = _minimal_ticks(frame)

    # Utility ei vaikuta luokitteluun lainkaan, ja tyhjä tapahtumataulu on
    # kelvollinen tulos -- toisin kuin tyhjä asetelmataulu. Kiinnike antaa siis
    # tyhjän mutta sopimuksen mukaisen taulun.
    tapahtumat = pl.DataFrame(
        schema={name: EVENTS[name] for name in EVENTS_ADAPTER_COLUMNS}
    )

    class Fake:
        def parse_demo(self, path: Path, sample_seconds) -> DemoTables:
            return DemoTables(rounds=adapteri, ticks=tickit, events=tapahtumat)

    parse_stage.run(
        parse_settings, arkisto, MAP_DEMO_ID, Fake(), demo_path=demo, force=force
    )


@pytest.fixture
def parsittu(arkisto: ArchivePaths, settings) -> ArchivePaths:
    parsi(arkisto, rounds_frame(ottelu()), settings.parse)
    return arkisto


def aja(settings, arkisto, team=A, **kwargs):
    return classify_stage.run(
        settings.thresholds, settings.league, arkisto, MAP_DEMO_ID, team, **kwargs
    )


# --- Onnistunut ajo --------------------------------------------------------------


def test_writes_a_valid_classified_table(settings, parsittu) -> None:
    tulos = aja(settings, parsittu)

    polku = parsittu.classified(A, MAP_DEMO_ID)
    assert polku.is_file()
    df = pl.read_parquet(polku)
    assert df.schema == dict(CLASSIFIED)
    assert df.height == 6, "yksi rivi per kierros, ei kahta"
    assert df["round_no"].to_list() == [1, 2, 3, 4, 5, 6]
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    assert tulos.status == "ok"
    assert not tulos.skipped
    assert tulos.stats["rounds"] == 6


def test_result_is_written_under_the_subject_team(settings, parsittu) -> None:
    aja(settings, parsittu, team=A)
    assert parsittu.classified(A, MAP_DEMO_ID).is_file()
    assert not parsittu.classified(B, MAP_DEMO_ID).exists()


def test_every_row_carries_a_reason_and_its_inputs(settings, parsittu) -> None:
    """Ilman perustelua ja lähtöarvoja kalibrointi Story 1.4:ssä on mahdotonta."""
    aja(settings, parsittu)
    df = pl.read_parquet(parsittu.classified(A, MAP_DEMO_ID))
    assert df["reason"].null_count() == 0
    assert all(len(r) > 20 for r in df["reason"].to_list())
    for inputs in df["inputs"].to_list():
        assert inputs["players"] == 5
        assert inputs["full_equip_min"] == settings.thresholds.full_equip_min


def test_pistol_round_is_classified_from_the_round_number(settings, parsittu) -> None:
    aja(settings, parsittu)
    df = pl.read_parquet(parsittu.classified(A, MAP_DEMO_ID))
    assert df.filter(pl.col("round_no") == 1)["round_type"].to_list() == ["pistol"]


def test_both_teams_are_classified_in_the_same_run(settings, parsittu) -> None:
    """``opp_round_type`` on toisen joukkueen oma ``round_type`` samalta ajolta."""
    aja(settings, parsittu, team=A)
    aja(settings, parsittu, team=B)

    a = pl.read_parquet(parsittu.classified(A, MAP_DEMO_ID)).sort("round_no")
    b = pl.read_parquet(parsittu.classified(B, MAP_DEMO_ID)).sort("round_no")

    assert a["round_type"].to_list() == b["opp_round_type"].to_list()
    assert b["round_type"].to_list() == a["opp_round_type"].to_list()
    assert a["side"].to_list() != b["side"].to_list()


def test_loss_count_is_written_per_round(settings, parsittu) -> None:
    aja(settings, parsittu)
    df = pl.read_parquet(parsittu.classified(A, MAP_DEMO_ID)).sort("round_no")
    assert df["loss_count"][0] == settings.thresholds.loss_count_half_start
    assert df["loss_count"].is_between(
        settings.thresholds.loss_count_min, settings.thresholds.loss_count_max
    ).all()


def test_league_and_roster_fields_stay_empty_until_epic_three(
    settings, parsittu
) -> None:
    """Arvaus olisi tässä pahempi kuin tyhjä: tieto tulee joukkueindeksistä."""
    aja(settings, parsittu)
    df = pl.read_parquet(parsittu.classified(A, MAP_DEMO_ID))
    assert df["is_league"].null_count() == df.height
    assert df["roster_class"].null_count() == df.height


def test_write_is_atomic(settings, parsittu) -> None:
    aja(settings, parsittu)
    assert not has_temp_leftovers(parsittu.root)


def test_nothing_is_written_into_the_parsed_area(settings, parsittu) -> None:
    """``classify`` ei kirjoita toisen vaiheen tulosalueelle."""
    ennen = {
        p: p.stat().st_mtime_ns
        for p in (parsittu.root / "parsed").rglob("*")
        if p.is_file()
    }
    aja(settings, parsittu)
    jalkeen = {
        p: p.stat().st_mtime_ns
        for p in (parsittu.root / "parsed").rglob("*")
        if p.is_file()
    }
    assert ennen == jalkeen


# --- Kierroslista Markdownina ----------------------------------------------------


def test_writes_a_readable_round_list_beside_the_table(settings, parsittu) -> None:
    tulos = aja(settings, parsittu)
    polku = parsittu.classified_round_list(A, MAP_DEMO_ID)
    assert polku.is_file()
    teksti = polku.read_text(encoding="utf-8")

    assert MAP_DEMO_ID in teksti
    assert teksti.count("\n|") >= 6, "rivi jokaiselle kierrokselle"
    # Kynnykset ovat mukana, muuten lista ei kerro mitä vasten päätös tehtiin.
    assert str(settings.thresholds.full_equip_min) in teksti
    assert str(settings.league.ot_start_money) in teksti
    assert str(polku.relative_to(parsittu.root)).replace("\\", "/") in [
        str(o) for o in tulos.outputs
    ]


def test_round_list_is_listed_as_an_output_in_the_manifest(settings, parsittu) -> None:
    aja(settings, parsittu)
    manifest = Manifest.read(parsittu.classified_manifest(A, MAP_DEMO_ID))
    assert any(o.endswith(".md") for o in manifest.outputs)
    assert any(o.endswith(".parquet") for o in manifest.outputs)


# --- Manifesti ja ohitus ---------------------------------------------------------


def test_manifest_has_no_tool_versions(settings, parsittu) -> None:
    """Luokittelu on puhdasta domain-laskentaa: mikään kirjastoversio ei muuta sitä."""
    aja(settings, parsittu)
    manifest = Manifest.read(parsittu.classified_manifest(A, MAP_DEMO_ID))
    assert manifest.stage == "classify"
    assert manifest.tool_versions == {}
    assert manifest.inputs[0].result_id == f"parsed/{MAP_DEMO_ID}"


def test_second_run_is_skipped(settings, parsittu) -> None:
    aja(settings, parsittu)
    polku = parsittu.classified(A, MAP_DEMO_ID)
    ennen = polku.stat().st_mtime_ns

    tulos = aja(settings, parsittu)
    assert tulos.skipped
    assert polku.stat().st_mtime_ns == ennen
    assert tulos.stats["rounds"] == 6
    assert tulos.stats["rows"], "kierroslista luetaan valmiista tuloksesta"


def test_force_overrides_a_matching_manifest(settings, parsittu) -> None:
    aja(settings, parsittu)
    assert not aja(settings, parsittu, force=True).skipped


def test_threshold_change_reruns_classify_but_not_parse(
    tmp_path: Path, arkisto
) -> None:
    """Hyväksymiskriteeri: kynnysmuutos ajaa luokittelun, ei parsintaa."""
    perus = tmp_path / "perus.toml"
    perus.write_text(settings_text(arkisto.root), encoding="utf-8")
    muutettu = tmp_path / "muutettu.toml"
    muutettu.write_text(
        settings_text(
            arkisto.root, **{"full_equip_min = 4000": "full_equip_min = 4100"}
        ),
        encoding="utf-8",
    )
    a = load_settings(perus, env_files=())
    b = load_settings(muutettu, env_files=())

    parsi(arkisto, rounds_frame(ottelu()), a.parse)
    parse_ennen = arkisto.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns

    aja(a, arkisto)
    tulos = aja(b, arkisto)

    assert not tulos.skipped, "kynnysmuutoksen jälkeen luokittelu ajetaan uudelleen"
    assert arkisto.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns == (
        parse_ennen
    ), "parsintaa ei saa ajaa uudelleen"


def test_a_forced_reparse_with_the_same_result_does_not_rerun_classify(
    settings, parsittu
) -> None:
    """Luokittelun syöte on parsinnan **tulos**, ei sen ajohetki.

    Ilman tätä jokainen ``parse --pakota`` pakottaisi myös uuden luokittelun,
    vaikka kierrostaulu olisi tavu tavulta sama.
    """
    aja(settings, parsittu)
    parsi(parsittu, rounds_frame(ottelu()), settings.parse, force=True)
    assert aja(settings, parsittu).skipped


def test_a_changed_demo_forces_a_new_classification(settings, parsittu) -> None:
    """Uusi parsinta uudesta demosta ei saa jäädä vanhan luokittelun taakse."""
    aja(settings, parsittu)
    demo = parsittu.import_dir() / f"{MAP_DEMO_ID}.dem"
    demo.write_bytes(b"PBDEMS2\x00" + b"y" * 4096)
    parsi(parsittu, rounds_frame(ottelu()), settings.parse)

    assert not aja(settings, parsittu).skipped


def test_missing_output_forces_a_rerun(settings, parsittu) -> None:
    aja(settings, parsittu)
    parsittu.classified(A, MAP_DEMO_ID).unlink()
    assert not aja(settings, parsittu).skipped


def test_unreadable_result_is_recomputed_not_reported(settings, parsittu) -> None:
    """Luokittelu on halpaa: rikkinäinen tulos lasketaan uudelleen."""
    aja(settings, parsittu)
    parsittu.classified(A, MAP_DEMO_ID).write_bytes(b"ei parquetia")

    tulos = aja(settings, parsittu)
    assert not tulos.skipped
    assert tulos.stats["rounds"] == 6
    assert pl.read_parquet(parsittu.classified(A, MAP_DEMO_ID)).height == 6


def test_result_that_no_longer_matches_the_contract_is_recomputed(
    settings, parsittu
) -> None:
    """Täsmäävä manifesti ei riitä, jos tulostaulun sopimus on muuttunut.

    Skeeman laajentuminen ei muuta manifestin sisältöä, joten vanha tulos
    näyttäisi ajantasaiselta mutta siitä puuttuisivat uudet arvot.
    """
    aja(settings, parsittu)
    polku = parsittu.classified(A, MAP_DEMO_ID)
    pl.read_parquet(polku).drop("loss_count").write_parquet(polku)

    tulos = aja(settings, parsittu)
    assert not tulos.skipped
    assert "loss_count" in pl.read_parquet(polku).columns


# --- Joukkueen valinta -----------------------------------------------------------


def test_team_can_be_given_as_a_unique_prefix(settings, parsittu) -> None:
    aja(settings, parsittu, team=A[:6])
    assert parsittu.classified(A, MAP_DEMO_ID).is_file()


def test_unknown_team_lists_both_lineups_of_the_demo(settings, parsittu) -> None:
    with pytest.raises(PappascoutError) as exc:
        aja(settings, parsittu, team="eitallaista")
    viesti = str(exc.value)
    assert A in viesti
    assert B in viesti
    assert "ei täsmää" in viesti


def test_missing_team_lists_both_lineups_too(settings, parsittu) -> None:
    with pytest.raises(PappascoutError) as exc:
        aja(settings, parsittu, team=None)
    viesti = str(exc.value)
    assert A in viesti and B in viesti
    assert "--team" in viesti


def test_ambiguous_prefix_is_refused(settings, arkisto) -> None:
    """Yhteinen alkuosa ei saa valita kokoonpanoa arpomalla."""
    frame = rounds_frame(ottelu()).with_columns(
        pl.when(pl.col("lineup_key") == A)
        .then(pl.lit("yhteinen1"))
        .otherwise(pl.lit("yhteinen2"))
        .alias("lineup_key")
    )
    parsi(arkisto, frame, settings.parse)
    with pytest.raises(PappascoutError, match="useampaan"):
        aja(settings, arkisto, team="yhteinen")


# --- Virheet ---------------------------------------------------------------------


def test_unparsed_demo_tells_which_command_to_run(settings, arkisto) -> None:
    with pytest.raises(PappascoutError) as exc:
        aja(settings, arkisto)
    viesti = str(exc.value)
    assert "ei ole vielä parsittu" in viesti
    assert "pappascout parse" in viesti


def test_failed_parse_is_not_classified_over(settings, parsittu) -> None:
    manifest = Manifest.read(parsittu.parsed_manifest(MAP_DEMO_ID))
    rikki = manifest.model_copy(
        update={"status": "parse_failed", "reason": "demo katkennut"}
    )
    rikki.write(parsittu.parsed_manifest(MAP_DEMO_ID))

    with pytest.raises(PappascoutError) as exc:
        aja(settings, parsittu)
    assert "parse_failed" in str(exc.value)
    assert "demo katkennut" in str(exc.value)


def test_missing_parse_manifest_is_a_finnish_error(settings, parsittu) -> None:
    parsittu.parsed_manifest(MAP_DEMO_ID).unlink()
    with pytest.raises(PappascoutError, match="manifestia ei löytynyt"):
        aja(settings, parsittu)


def test_rounds_table_that_breaks_the_contract_is_refused(
    settings, parsittu
) -> None:
    polku = parsittu.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(polku).drop("survivors").write_parquet(polku)
    with pytest.raises(SchemaError, match="survivors"):
        aja(settings, parsittu)


def test_a_round_without_an_anchor_does_not_break_the_run(
    settings, arkisto
) -> None:
    """I/O-matriisi: ankkuriton kierros jää luokittelematta, ajo jatkuu."""
    kierrokset = ottelu()
    kierrokset[2] = kierros(3, status="no_freeze_end")
    parsi(arkisto, rounds_frame(kierrokset), settings.parse)

    tulos = aja(settings, arkisto)
    df = pl.read_parquet(arkisto.classified(A, MAP_DEMO_ID)).sort("round_no")

    assert df.height == 6
    assert df["round_type"][2] is None
    assert "no_freeze_end" in df["reason"][2]
    assert df["round_type"].null_count() == 1
    assert tulos.stats["unclassified"] == 1


def test_short_handed_team_is_divided_by_the_observed_count(
    settings, arkisto
) -> None:
    """Vajaa joukkue: per pelaaja -arvo lasketaan oikealla määrällä."""
    summa = 4 * settings.thresholds.full_equip_min
    kierrokset = ottelu()
    kierrokset[3] = kierros(4, a_won=False, a_equip=summa, a_players=4)
    parsi(arkisto, rounds_frame(kierrokset), settings.parse)

    aja(settings, arkisto)
    df = pl.read_parquet(arkisto.classified(A, MAP_DEMO_ID)).sort("round_no")
    rivi = df.row(3, named=True)
    assert rivi["inputs"]["players"] == 4
    assert rivi["round_type"] == "full"




# --- Katselmuksen nostamat reunatapaukset ----------------------------------------


def test_unnumbered_rounds_are_dropped_and_counted(settings, arkisto) -> None:
    """Numeroimaton rivi kaataisi loss countin; se pudotetaan ja kerrotaan.

    Kierrostaulu kirjoitetaan tässä suoraan, koska ``parse`` ei itse päästä
    numeroimatonta riviä läpi -- mutta arkistossa voi olla vanhemmalla
    versiolla kirjoitettu taulu, eikä luokittelu saa kaatua siihen.
    """
    parsi(arkisto, rounds_frame(ottelu()), settings.parse)
    polku = arkisto.parsed_table(MAP_DEMO_ID, "rounds")
    taulu = pl.read_parquet(polku)
    numeroimaton = taulu.head(2).with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no"),
        pl.lit(99, dtype=pl.Int32).alias("round_raw"),
    )
    pl.concat([numeroimaton, taulu]).write_parquet(polku)

    tulos = aja(settings, arkisto)
    assert tulos.stats["unnumbered"] == 1
    assert tulos.stats["rounds"] == 6
    df = pl.read_parquet(arkisto.classified(A, MAP_DEMO_ID))
    assert df["round_no"].null_count() == 0


def test_skipped_run_gives_exactly_the_same_round_list(settings, parsittu) -> None:
    """Yksi polku kierroslistalle: ohitus ei saa näyttää eri lukuja.

    Jos tuore ja ohitettu ajo rakentaisivat rivit eri tavalla, ``--show``
    näyttäisi toisella ajolla esimerkiksi vastustajan talouden subjektin
    kierroksilla -- eikä mikään kertoisi siitä.
    """
    tuore = aja(settings, parsittu)
    ohitettu = aja(settings, parsittu)

    assert ohitettu.skipped
    assert not tuore.skipped
    assert ohitettu.stats["rows"] == tuore.stats["rows"]
    assert ohitettu.stats["by_type"] == tuore.stats["by_type"]


def test_league_change_reruns_classify_but_not_parse(tmp_path: Path, arkisto) -> None:
    """``[league]`` on osa luokittelun parametrihashia siinä missä kynnyksetkin."""
    perus = tmp_path / "perus.toml"
    perus.write_text(settings_text(arkisto.root), encoding="utf-8")
    muutettu = tmp_path / "muutettu.toml"
    muutettu.write_text(
        settings_text(
            arkisto.root, **{"ot_start_money = 12500": "ot_start_money = 10000"}
        ),
        encoding="utf-8",
    )
    a = load_settings(perus, env_files=())
    b = load_settings(muutettu, env_files=())

    parsi(arkisto, rounds_frame(ottelu()), a.parse)
    parse_ennen = arkisto.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns

    aja(a, arkisto)
    tulos = aja(b, arkisto)

    assert not tulos.skipped
    assert arkisto.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns == (
        parse_ennen
    )


def test_markdown_row_matches_the_parquet_row(settings, parsittu) -> None:
    """Taulukon sisältö, ei vain sen muoto: tyyppi ja perustelu ovat samat."""
    aja(settings, parsittu)
    teksti = parsittu.classified_round_list(A, MAP_DEMO_ID).read_text(encoding="utf-8")
    df = pl.read_parquet(parsittu.classified(A, MAP_DEMO_ID)).sort("round_no")
    odotettu = df.row(0, named=True)

    rivi = next(r for r in teksti.splitlines() if r.startswith("| 1 |"))
    solut = [s.strip() for s in rivi.strip("|").split("|")]
    otsikot = [o for o, _ in classify_stage.ROUND_LIST_COLUMNS]
    kentat = dict(zip(otsikot, solut))

    assert kentat["Tyyppi"] == str(odotettu["round_type"])
    assert kentat["Vast."] == str(odotettu["opp_round_type"])
    assert kentat["Loss"] == str(odotettu["loss_count"])
    assert kentat["Puoli"] == str(odotettu["side"])
    # Perustelu on sama teksti, vain Markdown-suojaukset poistettuna.
    assert kentat["Perustelu"].replace("\\", "") == str(odotettu["reason"]).replace(
        "\\", ""
    )
    # Ja per pelaaja -arvot vastaavat inputs-rakennetta.
    inputs = odotettu["inputs"]
    assert kentat["Varusteet"] == str(
        per_player(inputs["equip_freeze_end"], inputs["players"])
    )


def test_markdown_escapes_everything_that_would_break_the_table(settings) -> None:
    """Rivinvaihto rikkoisi taulukon ja backtick söisi loput rivistä."""
    rivit = [
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
    teksti = classify_stage.render_round_list_markdown(
        rivit,
        map_demo_id=MAP_DEMO_ID,
        team_key=A,
        thresholds=settings.thresholds,
        league=settings.league,
    )
    taulukkorivit = [r for r in teksti.splitlines() if r.startswith("| 1 |")]
    assert len(taulukkorivit) == 1, "rivinvaihto ei saa katkaista solua"
    rivi = taulukkorivit[0]
    assert rivi.count("|") == len(classify_stage.ROUND_LIST_COLUMNS) + 1 + 1
    assert "\\`" in rivi
    assert "\\|" in rivi


def test_markdown_is_byte_identical_on_a_rerun(settings, parsittu) -> None:
    """Ajohetki kuuluu manifestiin, ei tulosteeseen -- muuten erot eivät näy."""
    aja(settings, parsittu)
    ennen = parsittu.classified_round_list(A, MAP_DEMO_ID).read_bytes()
    aja(settings, parsittu, force=True)
    assert parsittu.classified_round_list(A, MAP_DEMO_ID).read_bytes() == ennen
    # Aikaleima on kuitenkin tallessa.
    assert Manifest.read(parsittu.classified_manifest(A, MAP_DEMO_ID)).created_at


def test_rounds_table_of_another_demo_is_refused(settings, parsittu) -> None:
    """Väärä parquet oikeassa polussa luokiteltaisiin väärän tunnisteen alle."""
    polku = parsittu.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(polku).with_columns(
        pl.lit("1-toinen-demo-1").alias("map_demo_id")
    ).write_parquet(polku)

    with pytest.raises(PappascoutError) as exc:
        aja(settings, parsittu)
    assert "toisen demon rivejä" in str(exc.value)
    assert "1-toinen-demo-1" in str(exc.value)


def test_three_lineups_are_refused_with_the_right_count(settings, arkisto) -> None:
    kierrokset = ottelu(4)
    kierrokset[3][1]["lineup_key"] = "cccccccccccccccc"
    parsi(arkisto, rounds_frame(kierrokset), settings.parse)

    with pytest.raises(SchemaError) as exc:
        aja(settings, arkisto)
    viesti = str(exc.value)
    assert "3 kokoonpanoa" in viesti
    assert "cccccccccccccccc" in viesti


def test_round_number_mismatch_between_teams_is_refused(settings, parsittu) -> None:
    """Ilman tarkistusta vastustajan tyyppi liittyisi väärälle riville."""
    polku = parsittu.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(polku).with_columns(
        pl.when((pl.col("lineup_key") == B) & (pl.col("round_no") == 6))
        .then(pl.lit(7, dtype=pl.Int32))
        .otherwise(pl.col("round_no"))
        .alias("round_no")
    ).write_parquet(polku)

    with pytest.raises(SchemaError, match="eivät täsmää"):
        aja(settings, parsittu)


def test_team_keys_lists_both_lineups(settings, parsittu) -> None:
    assert classify_stage.team_keys(parsittu, MAP_DEMO_ID) == sorted([A, B])


def test_inputs_carry_the_money_that_was_available(settings, parsittu) -> None:
    """Story 1.4 tarvitsee käytettävissä olleen rahan, ei vain jäljelle jäänyttä."""
    aja(settings, parsittu)
    df = pl.read_parquet(parsittu.classified(A, MAP_DEMO_ID))
    for inputs in df["inputs"].to_list():
        assert inputs["money_spent"] == 20000
        assert inputs["money_freeze_end"] + inputs["money_spent"] == 25000
        assert inputs["eco_loss_count_min"] == settings.thresholds.eco_loss_count_min
        assert inputs["eco_money_max_applied"] in (
            settings.thresholds.eco_money_max,
            settings.thresholds.eco_money_max_low_loss,
        )


# --- Oikeat demot ----------------------------------------------------------------


def real_rounds(demo_nimi: str, map_demo_id: str) -> pl.DataFrame:
    """Oikean demon kierrostaulu ``ROUNDS``-muodossa, ilman arkistoa."""
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    # Yksi näytepiste riittää: tämä apuri käyttää vain kierrostaulua, ja
    # portti palauttaa molemmat samasta lukukerrasta. Poissulkulista on
    # tuotannon, jotta adapteri ajetaan samoilla säännöillä kuin oikeasti.
    asetukset = load_settings(REAL_SETTINGS, env_files=()).parse
    adapteri = Demoparser2Adapter(
        exclude_weapons=asetukset.first_contact_exclude_weapons,
        fallback_death=asetukset.first_contact_fallback_death,
    )
    tables = adapteri.parse_demo(require_demo(demo_nimi), (6.0,))
    raaka = mark_played_rounds(tables.rounds)
    df = raaka.filter(pl.col("round_no").is_not_null()).select(
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
    df_, rivit = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "ancient"
    )
    assert df_.height == ANCIENT_ROUNDS
    assert df_.sort("round_no")["round_type"].to_list()[:3] == ["pistol", "eco", "full"]
    # Perustelu kertoo rahan ja loss countin jokaisella kierroksella.
    assert all("loss count" in str(r["reason"]) for r in rivit)


@pytest.mark.demo
def test_ancient_has_no_unclassified_rounds(settings_file: Path) -> None:
    thresholds = load_settings(settings_file, env_files=()).thresholds
    df = real_rounds(ANCIENT_DEM, "ancient")
    tulos, _ = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "ancient"
    )
    assert tulos["round_type"].null_count() == 0


@pytest.mark.demo
def test_nuke_overtime_rounds_get_no_economy_reasoning(settings_file: Path) -> None:
    thresholds = load_settings(settings_file, env_files=()).thresholds
    df = real_rounds(NUKE_ZST, "nuke")
    tulos, _ = classify_stage.classify_rounds(df, subject_key(df), thresholds, "nuke")

    assert tulos.height == NUKE_ROUNDS
    jatkoaika = tulos.filter(pl.col("round_no") > thresholds.regulation_rounds)
    assert sorted(jatkoaika["round_no"].to_list()) == [25, 26, 27, 28]
    assert set(jatkoaika["round_type"].to_list()) == {"ot"}
    assert set(jatkoaika["opp_round_type"].to_list()) == {"ot"}
    assert all("jatkoaikaa" in r for r in jatkoaika["reason"].to_list())


@pytest.mark.demo
def test_nuke_first_three_rounds_are_pistol_eco_full(settings_file: Path) -> None:
    thresholds = load_settings(settings_file, env_files=()).thresholds
    df = real_rounds(NUKE_ZST, "nuke")
    tulos, _ = classify_stage.classify_rounds(df, subject_key(df), thresholds, "nuke")
    assert tulos.sort("round_no")["round_type"].to_list()[:3] == [
        "pistol",
        "eco",
        "full",
    ]


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_nimi,tunniste", [(ANCIENT_DEM, "ancient"), (NUKE_ZST, "nuke")]
)
def test_opponent_type_matches_the_other_teams_own_type(
    settings_file: Path, demo_nimi: str, tunniste: str
) -> None:
    thresholds = load_settings(settings_file, env_files=()).thresholds
    df = real_rounds(demo_nimi, tunniste)
    a = subject_key(df)
    b = next(k for k in df["lineup_key"].unique().to_list() if k != a)

    oma, _ = classify_stage.classify_rounds(df, a, thresholds, tunniste)
    toinen, _ = classify_stage.classify_rounds(df, b, thresholds, tunniste)

    assert oma.sort("round_no")["round_type"].to_list() == (
        toinen.sort("round_no")["opp_round_type"].to_list()
    )
