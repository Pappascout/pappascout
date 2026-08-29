"""Arkiston polkujen testit (AD-7).

Kaikki manifesteihin ja indekseihin tallennettavat polut ovat suhteellisia --
absoluuttinen polku rikkoisi arkiston toisella koneella.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from pappascout.archive import paths
from pappascout.archive.atomic_write import atomic_write_bytes
from pappascout.archive.paths import ARCHIVE_ROOT_ENV_VAR, ArchivePaths, safe_component
from pappascout.errors import PappascoutError

RELATIVE_FUNCS = [
    (paths.raw_faceit_dir, ()),
    (paths.teams_index, ()),
    (paths.matches_index, ()),
    (paths.selection, ("team-abc",)),
    (paths.next_opponent, ("team-abc",)),
    (paths.demo, ("1234-0",)),
    (paths.demo_meta, ("1234-0",)),
    (paths.parsed_dir, ("1234-0",)),
    (paths.parsed_manifest, ("1234-0",)),
    (paths.classified, ("team-abc", "1234-0")),
    (paths.classified_manifest, ("team-abc", "1234-0")),
    (paths.report_json, ("team-abc",)),
    (paths.report_manifest, ("team-abc",)),
    (paths.reports_dir, ("team-abc",)),
    (paths.import_dir, ()),
    (paths.logs_dir, ("tyopoyta",)),
]


@pytest.mark.parametrize(
    "func,args", RELATIVE_FUNCS, ids=lambda v: getattr(v, "__name__", "")
)
def test_paths_are_relative_and_posix(func, args) -> None:
    result = func(*args)
    assert isinstance(result, PurePosixPath)
    text = str(result)
    assert not result.is_absolute()
    assert "\\" not in text
    assert ":" not in text


def test_archive_tree_matches_the_convention() -> None:
    assert str(paths.teams_index()) == "index/teams.json"
    assert str(paths.matches_index()) == "index/matches.json"
    assert str(paths.selection("t")) == "index/selections/t.json"
    assert str(paths.next_opponent("t")) == "index/next_opponent/t.json"
    assert str(paths.demo("1234-0")) == "demos/1234-0.dem.zst"
    assert str(paths.demo_meta("1234-0")) == "demos/1234-0.meta.json"
    assert str(paths.parsed_table("1234-0", "rounds")) == "parsed/1234-0/rounds.parquet"
    assert str(paths.classified("t", "1234-0")) == "classified/t/1234-0.parquet"
    assert str(paths.report_json("t")) == "aggregates/t/report.json"
    assert str(paths.reports_dir("t")) == "reports/t"
    assert str(paths.logs_dir("kone")) == "logs/kone"
    assert str(paths.LOCK_FILE) == ".lock"


def test_map_demo_id_is_match_id_and_zero_based_map_index() -> None:
    """map_demo_id = {match_id}-{map_index}, karttaindeksi 0-pohjainen."""
    assert str(paths.demo("1-8ffb4c53-0")).endswith("1-8ffb4c53-0.dem.zst")


@pytest.mark.parametrize("table", ["rounds", "ticks", "events"])
def test_parse_writes_exactly_three_tables(table: str) -> None:
    assert table in paths.PARSED_TABLES
    assert str(paths.parsed_table("d", table)).endswith(f"{table}.parquet")


def test_unknown_parsed_table_is_rejected() -> None:
    with pytest.raises(ValueError) as exc:
        paths.parsed_table("d", "kierrokset")
    assert "kierrokset" in str(exc.value)


# --- Tunnisteiden tarkistus ---------------------------------------------------


@pytest.mark.parametrize(
    "unsafe",
    ["..", ".", "../..", "a/b", "a\\b", "", "C:", "x" * 121, "team key", "a:b"],
)
def test_unsafe_identifiers_are_rejected(unsafe: str) -> None:
    """Tunniste ei saa karata arkiston juuresta eikä rikkoa polkua."""
    with pytest.raises(PappascoutError):
        safe_component(unsafe, "team_key")


@pytest.mark.parametrize("safe", ["team-abc", "1234-0", "1-8ffb4c53-0", "kone_1", "a.b"])
def test_safe_identifiers_pass_through(safe: str) -> None:
    assert safe_component(safe, "team_key") == safe


def test_path_builders_reject_traversal() -> None:
    """Polunrakentajat tarkistavat tunnisteen -- ei vain safe_component itse."""
    with pytest.raises(PappascoutError):
        paths.logs_dir("../..")
    with pytest.raises(PappascoutError):
        paths.selection("../../salaisuudet")
    with pytest.raises(PappascoutError):
        paths.demo("../../../etc/passwd")
    with pytest.raises(PappascoutError):
        paths.classified("ok-tiimi", "..")


# --- Juuren liittäminen -------------------------------------------------------


def test_resolve_and_relative_round_trip(tmp_path: Path) -> None:
    archive = ArchivePaths.from_settings(tmp_path)
    relative_path = paths.parsed_table("1234-0", "rounds")
    absolute_path = archive.resolve(relative_path)
    assert absolute_path == tmp_path / "parsed" / "1234-0" / "rounds.parquet"

    absolute_path.parent.mkdir(parents=True)
    absolute_path.write_bytes(b"x")
    assert archive.relative(absolute_path) == relative_path


def test_resolve_rejects_absolute_paths(tmp_path: Path) -> None:
    """Absoluuttinen polku manifestissa on aina virhe, ei hiljainen hyväksyntä."""
    archive = ArchivePaths.from_settings(tmp_path)
    for unsafe in ("C:/muualla/tulos.parquet", "/etc/passwd"):
        with pytest.raises(PappascoutError) as exc:
            archive.resolve(unsafe)
        assert "suhteellinen" in str(exc.value)


# --- Juuren laajennus ja ylikirjoitus ----------------------------------------


def test_from_settings_expands_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sama versioitu asetusrivi toimii molemmilla koneilla."""
    monkeypatch.setenv("TESTIKOTI", str(tmp_path))
    archive = ArchivePaths.from_settings("%TESTIKOTI%/arkisto")
    assert archive.root == tmp_path / "arkisto"


def test_from_settings_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = ArchivePaths.from_settings("~/pappascout-arkisto")
    assert archive.root == Path.home() / "pappascout-arkisto"
    assert "~" not in str(archive.root)


def test_env_var_overrides_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAPPASCOUT_ARCHIVE_ROOT ohjaa ajon toiseen arkistoon."""
    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, str(tmp_path / "toinen"))
    archive = ArchivePaths.from_settings("C:/asetuksen/arkisto")
    assert archive.root == tmp_path / "toinen"


def test_real_settings_root_expands_to_an_absolute_path() -> None:
    """Oikean asetustiedoston polku laajenee absoluuttiseksi tällä koneella."""
    from conftest import REAL_SETTINGS
    import tomllib

    data = tomllib.loads(REAL_SETTINGS.read_text(encoding="utf-8"))
    archive = ArchivePaths.from_settings(data["project"]["archive_root"])
    assert archive.root.is_absolute()
    assert "%" not in str(archive.root)


# --- Tilatiedot ---------------------------------------------------------------


def test_missing_archive_reports_zero_size(tmp_path: Path) -> None:
    archive = ArchivePaths.from_settings(tmp_path / "ei-ole")
    assert archive.exists() is False
    assert archive.total_size_bytes() == 0


def test_total_size_counts_files(tmp_path: Path) -> None:
    archive = ArchivePaths.from_settings(tmp_path)
    atomic_write_bytes(archive.resolve(paths.teams_index()), b"12345")
    atomic_write_bytes(archive.resolve(paths.matches_index()), b"123")
    assert archive.exists() is True
    assert archive.total_size_bytes() == 8


def test_find_demo_accepts_both_compressions(tmp_path: Path) -> None:
    """FACEIT tarjoaa .dem.zst; käsin tuotu voi olla .dem.gz."""
    archive = ArchivePaths.from_settings(tmp_path)
    assert archive.find_demo("1234-0") is None

    gz = archive.demo("1234-0", ".dem.gz")
    gz.parent.mkdir(parents=True, exist_ok=True)
    gz.write_bytes(b"x")
    assert archive.find_demo("1234-0") == gz

    zst = archive.demo("1234-0")
    zst.write_bytes(b"x")
    assert archive.find_demo("1234-0") == zst  # zst on ensisijainen
