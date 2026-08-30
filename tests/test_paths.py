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
    (paths.report_markdown, ("team-abc", "2026-08-30T0307-abc.md")),
    (paths.render_manifest, ("team-abc", "2026-08-30T0307-abc.md")),
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
    assert (
        str(paths.report_markdown("t", "2026-08-30T0307-t.md"))
        == "reports/t/2026-08-30T0307-t.md"
    )
    assert (
        str(paths.render_manifest("t", "2026-08-30T0307-t.md"))
        == "reports/t/2026-08-30T0307-t.manifest.json"
    )
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


@pytest.mark.parametrize(
    "unsafe",
    ["../pako.md", "a/b.md", "a\\b.md", "", "..", "C:/muualla.md"],
)
def test_report_file_names_are_checked_too(unsafe: str) -> None:
    """Raportin tiedostonimi tulee vaiheelta, ei tunnisteesta.

    Se on ainoa polun osa, jota ei johdeta ``team_key``:stä tai
    ``map_demo_id``:stä, joten sen tarkistus on erikseen todistettava --
    muuten aikaleiman muotoilun muuttaminen voisi karata arkiston juuresta
    ilman että mikään huomauttaisi.
    """
    with pytest.raises(PappascoutError):
        paths.report_markdown("ok-tiimi", unsafe)
    with pytest.raises(PappascoutError):
        paths.render_manifest("ok-tiimi", unsafe)


def test_the_manifest_name_is_derived_from_the_report_name() -> None:
    """Raporttikohtainen manifesti: kaksi rinnakkaista ajoa ei kirjoita samaan."""
    first = paths.render_manifest("t", "2026-08-30T0307-t.md")
    second = paths.render_manifest("t", "2026-08-30T0307-t-02.md")
    assert first != second
    assert str(first).endswith(".manifest.json")
    assert str(second).endswith("-02.manifest.json")


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


def test_unset_variable_is_an_error_not_a_literal_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Laajentamaton ``%NIMI%`` kaataa ajon eikä muutu hakemiston nimeksi.

    ``os.path.expandvars`` **jättää ``%NIMI%``:n sellaisenaan**, jos muuttujaa
    ei ole -- se ei nosta virhettä eikä palauta tyhjää. Ilman tätä
    tarkistusta ajo loi hakemiston, jonka nimi oli kirjaimellisesti
    ``%USERPROFILE%``, kirjoitti koko arkiston sinne ja näytti onnistuneen.
    Käyttäjällä on kaksi konetta, joten arkisto hajoaisi hiljaa.

    Näin oikeasti kävi: repoon syntyi ``%USERPROFILE%``-niminen hakemisto
    täysine polkupuineen.
    """
    monkeypatch.delenv("EI_ASETETTU_MUUTTUJA", raising=False)
    monkeypatch.delenv(ARCHIVE_ROOT_ENV_VAR, raising=False)

    with pytest.raises(PappascoutError) as exc:
        ArchivePaths.from_settings("%EI_ASETETTU_MUUTTUJA%/arkisto")

    message = str(exc.value)
    # Nimeää puuttuvan muuttujan ja kertoo mistä arvo tuli.
    assert "EI_ASETETTU_MUUTTUJA" in message
    assert "archive_root" in message


def test_unset_variable_in_the_env_override_names_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sama tarkistus koskee ympäristömuuttujan kautta annettua polkua.

    Viesti nimeää eri lähteen: käyttäjän on tiedettävä kumpaa korjata, kun
    ``PAPPASCOUT_ARCHIVE_ROOT`` ohittaa asetuksen kokonaan.
    """
    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, "%TOINEN_PUUTTUVA%/arkisto")
    monkeypatch.delenv("TOINEN_PUUTTUVA", raising=False)

    with pytest.raises(PappascoutError) as exc:
        ArchivePaths.from_settings("C:/ei-valia")

    message = str(exc.value)
    assert "TOINEN_PUUTTUVA" in message
    assert ARCHIVE_ROOT_ENV_VAR in message


def test_braced_variable_is_checked_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``${NIMI}`` on yhtä yksiselitteinen paikkamerkki kuin ``%NIMI%``."""
    monkeypatch.delenv(ARCHIVE_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv("KOLMAS_PUUTTUVA", raising=False)

    with pytest.raises(PappascoutError, match="KOLMAS_PUUTTUVA"):
        ArchivePaths.from_settings("${KOLMAS_PUUTTUVA}/arkisto")


def test_a_set_variable_still_expands_without_complaint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vartija ei saa hylätä polkuja, jotka laajenevat oikein.

    Ilman tätä paria edellinen testi menisi läpi myös toteutuksella, joka
    kaatuu jokaiseen prosenttimerkkiin.
    """
    monkeypatch.delenv(ARCHIVE_ROOT_ENV_VAR, raising=False)
    monkeypatch.setenv("ON_ASETETTU", str(tmp_path))
    archive = ArchivePaths.from_settings("%ON_ASETETTU%/arkisto")
    assert archive.root == tmp_path / "arkisto"


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
