"""Manifestin testit: vaiheiden ohitussopimus (AD-1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import has_temp_leftovers, settings_text
from pappascout.archive.manifest import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    ManifestInput,
    compute_params_hash,
    tool_versions,
)
from pappascout.domain.models import load_settings
from pappascout.errors import PappascoutError

TOOLS = {"demoparser2": "0.42.0"}
INPUTS = [
    ManifestInput(result_id="demo:1-0", sha256="a" * 64),
    ManifestInput(result_id="settings:parse", sha256="b" * 64),
]
OUTPUT = "parsed/1-0/rounds.parquet"


def _manifest(**overrides) -> Manifest:
    kwargs = dict(
        result_id="parsed:1-0",
        stage="parse",
        params_hash="hash-1",
        inputs=INPUTS,
        tool_versions=TOOLS,
        outputs=[OUTPUT],
    )
    kwargs.update(overrides)
    return Manifest.new(**kwargs)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """Arkiston juuri, jossa manifestin tulostiedosto on olemassa."""
    output = tmp_path / OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"parquet")
    return tmp_path


def test_round_trip_through_disk(tmp_path: Path) -> None:
    target = tmp_path / "parse.manifest.json"
    original = _manifest()
    original.write(target)

    read_back = Manifest.read(target)
    assert read_back.result_id == original.result_id
    assert read_back.stage == "parse"
    assert read_back.params_hash == "hash-1"
    assert read_back.tool_versions == TOOLS
    assert [i.key() for i in read_back.inputs] == [i.key() for i in INPUTS]
    assert read_back.status == "ok"
    assert not has_temp_leftovers(tmp_path)


def test_matching_manifest_allows_skip(archive: Path) -> None:
    """Täsmäävä manifesti -> vaihe ohitetaan."""
    m = _manifest()
    assert m.is_current(
        inputs=INPUTS, params_hash="hash-1", tool_versions=TOOLS, root=archive
    )


def test_input_order_does_not_matter(archive: Path) -> None:
    m = _manifest()
    assert m.is_current(
        inputs=list(reversed(INPUTS)),
        params_hash="hash-1",
        tool_versions=TOOLS,
        root=archive,
    )


def test_changed_params_hash_forces_rerun(archive: Path) -> None:
    """Kynnysten muutos näkyy params_hashissa ja pakottaa uudelleenajon."""
    m = _manifest()
    assert not m.is_current(
        inputs=INPUTS, params_hash="hash-2", tool_versions=TOOLS, root=archive
    )


def test_changed_input_forces_rerun(archive: Path) -> None:
    m = _manifest()
    changed = [ManifestInput(result_id="demo:1-0", sha256="c" * 64), INPUTS[1]]
    assert not m.is_current(
        inputs=changed, params_hash="hash-1", tool_versions=TOOLS, root=archive
    )


def test_missing_input_forces_rerun(archive: Path) -> None:
    m = _manifest()
    assert not m.is_current(
        inputs=INPUTS[:1], params_hash="hash-1", tool_versions=TOOLS, root=archive
    )


def test_changed_tool_version_forces_rerun(archive: Path) -> None:
    """demoparser2:n päivitys invalidoi parsinnan tuloksen."""
    m = _manifest()
    new = {"demoparser2": "0.43.0"}
    assert not m.is_current(
        inputs=INPUTS, params_hash="hash-1", tool_versions=new, root=archive
    )


@pytest.mark.parametrize(
    "status", ["no_demo", "download_failed", "parse_failed", "no_freeze_end", "pruned"]
)
def test_non_ok_status_is_never_current(status: str, archive: Path) -> None:
    """Vain onnistunut tulos kelpaa ohitukseen."""
    m = _manifest(status=status, reason="testi")
    assert not m.is_current(
        inputs=INPUTS, params_hash="hash-1", tool_versions=TOOLS, root=archive
    )


def test_timestamp_does_not_affect_currency(archive: Path) -> None:
    """Aikaleima ei ole osa vertailua -- muuten mikään ei ohittuisi koskaan."""
    old = _manifest()
    new = _manifest()
    assert new.created_at >= old.created_at
    for m in (old, new):
        assert m.is_current(
            inputs=INPUTS, params_hash="hash-1", tool_versions=TOOLS, root=archive
        )


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _manifest(status="ihan_uusi_tila")


# --- Tulostiedostojen olemassaolo (OneDrive) ---------------------------------


def test_missing_output_forces_rerun(tmp_path: Path) -> None:
    """Manifesti täsmää mutta tulos puuttuu levyltä -> ei saa ohittaa.

    OneDrive synkronoi pienen manifestin nopeasti mutta satojen megatavujen
    Parquetin myöhemmin; käyttäjä on myös voinut poistaa tiedoston käsin.
    """
    m = _manifest()
    assert m.missing_outputs(tmp_path) == [OUTPUT]
    assert not m.outputs_present(tmp_path)
    assert not m.is_current(
        inputs=INPUTS, params_hash="hash-1", tool_versions=TOOLS, root=tmp_path
    )


def test_output_deleted_after_the_fact_forces_rerun(archive: Path) -> None:
    m = _manifest()
    assert m.is_current(
        inputs=INPUTS, params_hash="hash-1", tool_versions=TOOLS, root=archive
    )
    (archive / OUTPUT).unlink()
    assert not m.is_current(
        inputs=INPUTS, params_hash="hash-1", tool_versions=TOOLS, root=archive
    )


def test_manifest_without_outputs_is_still_comparable(tmp_path: Path) -> None:
    m = _manifest(outputs=[])
    assert m.outputs_present(tmp_path)
    assert m.is_current(
        inputs=INPUTS, params_hash="hash-1", tool_versions=TOOLS, root=tmp_path
    )


# --- Skeemaversio -------------------------------------------------------------


def test_foreign_schema_version_is_not_current(archive: Path) -> None:
    m = _manifest().model_copy(update={"schema_version": "2.0.0"})
    assert not m.is_current(
        inputs=INPUTS, params_hash="hash-1", tool_versions=TOOLS, root=archive
    )


def test_newer_schema_version_has_its_own_message(tmp_path: Path) -> None:
    """Uudempi manifesti ei ole 'vioittunut' -- viesti kertoo oikean syyn."""
    target = tmp_path / "uusi.manifest.json"
    _manifest().model_copy(update={"schema_version": "2.0.0"}).write(target)
    with pytest.raises(PappascoutError) as exc:
        Manifest.read(target)
    message = str(exc.value)
    assert "uudemmalla versiolla" in message
    assert MANIFEST_SCHEMA_VERSION in message
    assert "vioittunut" not in message


def test_newer_schema_version_is_treated_as_missing(tmp_path: Path) -> None:
    target = tmp_path / "uusi.manifest.json"
    _manifest().model_copy(update={"schema_version": "2.0.0"}).write(target)
    assert Manifest.read_if_exists(target) is None


# --- Lukuvirheet --------------------------------------------------------------


def test_reading_missing_manifest_gives_finnish_error(tmp_path: Path) -> None:
    with pytest.raises(PappascoutError) as exc:
        Manifest.read(tmp_path / "ei-ole.manifest.json")
    assert "ei löytynyt" in str(exc.value)


def test_read_if_exists_returns_none_for_missing(tmp_path: Path) -> None:
    assert Manifest.read_if_exists(tmp_path / "ei-ole.json") is None


def test_read_if_exists_treats_corrupt_as_missing(tmp_path: Path) -> None:
    """Vioittunut manifesti ei kaada ajoa, vaan vaihe ajetaan uudelleen."""
    broken = tmp_path / "rikki.manifest.json"
    broken.write_text("{ ei ole jsonia", encoding="utf-8")
    assert Manifest.read_if_exists(broken) is None


def test_corrupt_manifest_read_says_what_to_do(tmp_path: Path) -> None:
    broken = tmp_path / "rikki.manifest.json"
    broken.write_text('{"result_id": "x"}', encoding="utf-8")
    with pytest.raises(PappascoutError) as exc:
        Manifest.read(broken)
    assert "vioittunut" in str(exc.value)


# --- tool_versions ------------------------------------------------------------


def test_tool_versions_reads_installed_packages() -> None:
    versions = tool_versions("demoparser2", "polars")
    assert set(versions) == {"demoparser2", "polars"}
    assert all(v and v[0].isdigit() for v in versions.values())


def test_tool_versions_rejects_unknown_package() -> None:
    with pytest.raises(PappascoutError) as exc:
        tool_versions("eioleolemassa-paketti")
    assert "uv sync" in str(exc.value)


def test_pappascout_version_is_not_a_tool_version() -> None:
    """Oma versionnosto ei saa invalidoida koko arkistoa.

    Sääntö on kirjattu manifest.py:n docstringiin: manifestiin merkitään vain
    ne työkalut, joiden versio oikeasti muuttaa vaiheen tuloksen.
    """
    assert "pappascout" not in tool_versions("demoparser2")


# --- params_hash --------------------------------------------------------------


def test_params_hash_ignores_key_order() -> None:
    a = compute_params_hash({"snapshot_seconds": [6, 15], "area_snap_units": None})
    b = compute_params_hash({"area_snap_units": None, "snapshot_seconds": [6, 15]})
    assert a == b


def test_params_hash_changes_with_value() -> None:
    a = compute_params_hash({"snapshot_seconds": [6, 15]})
    b = compute_params_hash({"snapshot_seconds": [6, 30]})
    assert a != b


def test_params_hash_is_stable_across_calls() -> None:
    params = {"full_equip_min": 4000, "pistol_rounds": [1, 13]}
    assert compute_params_hash(params) == compute_params_hash(params)
    assert len(compute_params_hash(params)) == 64


def test_params_hash_rejects_non_json_values() -> None:
    """WindowsPath merkkijonoutuisi koneriippuvasti -> eri hash eri koneella."""
    with pytest.raises(PappascoutError) as exc:
        compute_params_hash({"parse": {"archive_root": Path("C:/arkisto")}})
    message = str(exc.value)
    assert "parse.archive_root" in message
    assert "Path" in message


# --- AD-3:n ydinlupaus: kynnysmuutos ei uudelleenparsi ------------------------


def _parse_hash_for(settings_path: Path) -> str:
    """Laske parse-vaiheen parametrihash yhdestä asetustiedostosta."""
    s = load_settings(settings_path, env_files=())
    return compute_params_hash(
        {**s.parse.model_dump(mode="json"), **tool_versions("demoparser2")}
    )


def test_threshold_change_does_not_change_parse_hash(tmp_path: Path) -> None:
    """Kynnysten säätö EI saa muuttaa parse-hashia.

    Tämä on AD-3:n ydinlupaus ja koko Story 1.4:n perusta: kun käyttäjä säätää
    ``[thresholds]``-arvoa, parsinta ohitetaan ja tulos valmistuu sekunneissa.
    Kaksi asetustiedostoa, jotka eroavat VAIN kynnysosiossa, tuottavat siis
    saman parse-hashin.
    """
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text(settings_text(tmp_path / "arkisto"), encoding="utf-8")
    b.write_text(
        settings_text(
            tmp_path / "arkisto",
            **{"full_equip_min = 4000": "full_equip_min = 4500"},
        ),
        encoding="utf-8",
    )

    # Tiedostot eroavat oikeasti, ja ero on kynnysosiossa.
    assert a.read_text(encoding="utf-8") != b.read_text(encoding="utf-8")
    assert load_settings(a, env_files=()).thresholds.full_equip_min == 4000
    assert load_settings(b, env_files=()).thresholds.full_equip_min == 4500

    assert _parse_hash_for(a) == _parse_hash_for(b)


def test_parse_change_does_change_parse_hash(tmp_path: Path) -> None:
    """Näytepisteiden muutos sen sijaan PAKOTTAA uudelleenparsinnan."""
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text(settings_text(tmp_path / "arkisto"), encoding="utf-8")
    b.write_text(
        settings_text(
            tmp_path / "arkisto",
            **{
                "snapshot_seconds = [6.0, 15.0, 30.0, 45.0]": (
                    "snapshot_seconds = [6.0, 20.0, 30.0, 45.0]"
                )
            },
        ),
        encoding="utf-8",
    )

    assert load_settings(b, env_files=()).parse.snapshot_seconds == [
        6.0,
        20.0,
        30.0,
        45.0,
    ]
    assert _parse_hash_for(a) != _parse_hash_for(b)


def test_league_change_does_not_change_parse_hash(tmp_path: Path) -> None:
    """Uusi kausi ei pakota parsimaan vanhoja demoja uudelleen."""
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text(settings_text(tmp_path / "arkisto"), encoding="utf-8")
    b.write_text(
        settings_text(
            tmp_path / "arkisto",
            **{
                'championship_ids = ["94681888-b5da-4ab5-bf50-f44b666b98a3"]': (
                    'championship_ids = ["94681888-b5da-4ab5-bf50-f44b666b98a3", '
                    '"11111111-2222-3333-4444-555555555555"]'
                )
            },
        ),
        encoding="utf-8",
    )
    assert len(load_settings(b, env_files=()).league.championship_ids) == 2
    assert _parse_hash_for(a) == _parse_hash_for(b)
