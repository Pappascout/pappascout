"""Asetusten ja avainten testit -- I/O-matriisin rivit 5 ja 6.

Testit varmistavat myös, että ``settings.toml``in luvut ovat ne, jotka PRD:n
addendumissa ja domain-tutkimuksessa on todettu, ja että osioiden väliset
ristiriidat jäävät kiinni latausvaiheessa.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import REAL_SETTINGS, settings_text
from pappascout.domain.models import (
    SETTINGS_ENV_VAR,
    EconomySettings,
    LeagueSettings,
    ParseSettings,
    ProjectSettings,
    Settings,
    ThresholdSettings,
    load_settings,
    secrets_env_path,
    settings_search_paths,
)
from pappascout.errors import PappascoutError, SettingsError

FAKE_KEY = "kokeiluavain-1234567890"
FAKE_TOKEN = "kokeilutoken-abcdefghij"


def _load(settings_file: Path, env: Path | None = None) -> Settings:
    return load_settings(settings_file, env_files=(env,) if env else ())


def _write_variant(tmp_path: Path, **replacements: str) -> Path:
    """Kirjoita muunneltu settings.toml ja palauta polku."""
    target = tmp_path / "muunnos.toml"
    target.write_text(
        settings_text(tmp_path / "arkisto", **replacements), encoding="utf-8"
    )
    return target


# --- Rivi 5: asetukset puuttuvat -------------------------------------------


def test_missing_settings_file_names_the_path(tmp_path: Path) -> None:
    """settings.toml puuttuu -> suomenkielinen virhe, joka kertoo polun."""
    missing = tmp_path / "settings.toml"
    with pytest.raises(SettingsError) as exc:
        load_settings(missing)
    message = str(exc.value)
    assert str(missing) in message
    assert "ei löytynyt" in message


def test_search_lists_every_path_it_tried(
    isolated_cwd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kun tiedostoa ei löydy mistään, virhe listaa kaikki etsityt polut."""
    # Estä repon oma settings.toml, joka on hakujärjestyksen viimeinen varasija.
    monkeypatch.setattr(
        "pappascout.domain.models._repo_root", lambda: tmp_path / "ei-repoa"
    )
    with pytest.raises(SettingsError) as exc:
        load_settings()
    message = str(exc.value)
    assert "settings.toml" in message
    assert SETTINGS_ENV_VAR in message
    assert str(isolated_cwd / "settings.toml") in message


def test_repo_root_is_the_last_fallback(isolated_cwd: Path) -> None:
    """Komennon saa ajaa mistä tahansa hakemistosta -- repon juuri on varasija."""
    assert REAL_SETTINGS in settings_search_paths()
    assert load_settings().settings_file == REAL_SETTINGS


def test_settings_env_var_has_highest_priority(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, isolated_cwd: Path
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    assert settings_search_paths()[0] == settings_file
    assert load_settings().settings_file == settings_file


def test_broken_toml_says_what_to_fix(tmp_path: Path) -> None:
    broken = tmp_path / "settings.toml"
    broken.write_text("[project\nown_team_name = 1", encoding="utf-8")
    with pytest.raises(SettingsError) as exc:
        load_settings(broken)
    assert "TOML" in str(exc.value)


def test_unknown_section_is_not_silently_ignored(tmp_path: Path) -> None:
    """Kirjoitusvirhe osion nimessä on virhe, ei hiljainen oletusarvo."""
    target = tmp_path / "settings.toml"
    target.write_text(
        settings_text(tmp_path / "arkisto") + "\n[treshholds]\nfoo = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "treshholds" in str(exc.value)


def test_unknown_key_inside_a_section_is_rejected(tmp_path: Path) -> None:
    """Kirjoitusvirhe avaimen nimessä osion sisällä jää kiinni.

    Tämä on Story 1.4:n kalibrointityönkulun turvaverkko: kynnysarvoa säädetään
    käsin, ja väärin kirjoitettu avain jäisi muuten hiljaa vaikuttamatta
    mihinkään -- käyttäjä luulisi säätäneensä rajaa, vaikka koodi käyttää yhä
    vanhaa arvoa.
    """
    target = _write_variant(tmp_path, **{"full_equip_min = 4000": "full_equp_min = 4500"})
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    message = str(exc.value)
    assert "full_equp_min" in message
    assert "thresholds" in message


def test_secrets_cannot_be_put_in_settings_toml(tmp_path: Path) -> None:
    """Avain ei kuulu versioituun tiedostoon, joten se hylätään."""
    target = tmp_path / "settings.toml"
    target.write_text(
        settings_text(tmp_path / "arkisto") + '\nfaceit_api_key = "salainen"\n',
        encoding="utf-8",
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "faceit_api_key" in str(exc.value)


def test_invalid_value_is_reported_with_field_name(tmp_path: Path) -> None:
    target = _write_variant(tmp_path, **{"full_equip_min = 4000": "full_equip_min = -5"})
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "full_equip_min" in str(exc.value)


# --- Osioiden sisäiset ja väliset ristiriidat --------------------------------


def test_the_after_win_anomaly_bar_must_stay_below_a_full_buy(
    tmp_path: Path,
) -> None:
    """Muuten poikkeamaraja söisi täyden oston voiton jälkeiseltä haaralta."""
    target = _write_variant(
        tmp_path,
        **{"anomaly_equip_max_after_win = 2000": "anomaly_equip_max_after_win = 4000"},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "anomaly_equip_max_after_win" in str(exc.value)


def test_money_left_max_must_stay_below_the_purchase_threshold(
    tmp_path: Path,
) -> None:
    """P2: ilman tätä ``half`` on tavoittamaton eikä mikään huomauta.

    Jos taskuun saa jäädä enemmän rahaa kuin ostaminen ylipäätään vaatii,
    jokainen hävityn jälkeinen ostos on force.
    """
    target = _write_variant(
        tmp_path,
        **{"force_money_left_max = 1000": "force_money_left_max = 100000"},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "force_money_left_max" in str(exc.value)


def test_force_buy_min_must_stay_below_a_full_buy(tmp_path: Path) -> None:
    """Muuten forcea eikä puoliostoa voisi koskaan saavuttaa."""
    target = _write_variant(
        tmp_path, **{"force_buy_min = 1500": "force_buy_min = 4000"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "force_buy_min" in str(exc.value)


def test_a_retired_threshold_left_in_settings_is_refused(tmp_path: Path) -> None:
    """I/O-matriisi: ``force_money_max`` poistui, joten sen jättäminen on virhe.

    ``extra="forbid"`` tekee puolittaisesta siivouksesta ajonaikaisen virheen,
    ei hiljaista jäännettä: käyttäjä luulisi muuten säätävänsä rajaa, jolla ei
    ole enää lukijaa.
    """
    target = _write_variant(
        tmp_path,
        **{"force_money_left_max = 1000": "force_money_left_max = 1000\nforce_money_max = 2500"},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    message = str(exc.value)
    assert "force_money_max" in message
    assert "thresholds" in message


def test_loss_count_min_must_be_below_max(tmp_path: Path) -> None:
    target = _write_variant(
        tmp_path, **{"loss_count_min = 0": "loss_count_min = 4"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "loss_count_min" in str(exc.value)


def test_roster_min_regulars_cannot_exceed_roster_size(tmp_path: Path) -> None:
    target = _write_variant(
        tmp_path, **{"roster_min_regulars = 4": "roster_min_regulars = 6"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "roster_min_regulars" in str(exc.value)


def test_loss_bonus_steps_must_match_loss_count_max(tmp_path: Path) -> None:
    """Laskuri indeksoi porrastaulukkoa suoraan, joten pituuden on täsmättävä."""
    target = _write_variant(
        tmp_path,
        **{
            "loss_bonus_steps = [1400, 1900, 2400, 2900, 3400]": (
                "loss_bonus_steps = [1400, 1900, 2400]"
            )
        },
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "loss_bonus_steps" in str(exc.value)


def test_loss_bonus_steps_must_ascend(tmp_path: Path) -> None:
    target = _write_variant(
        tmp_path,
        **{
            "loss_bonus_steps = [1400, 1900, 2400, 2900, 3400]": (
                "loss_bonus_steps = [1400, 1900, 1900, 2900, 3400]"
            )
        },
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "loss_bonus_steps" in str(exc.value)


def test_regulation_rounds_must_match_mr(tmp_path: Path) -> None:
    """MR12 tarkoittaa 24 säännönmukaista kierrosta -- ristiriita on virhe."""
    target = _write_variant(
        tmp_path, **{"regulation_rounds = 24": "regulation_rounds = 30"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "regulation_rounds" in str(exc.value)


def test_pistol_rounds_must_match_mr(tmp_path: Path) -> None:
    """MR12:ssa pistoolikierrokset ovat 1 ja 13, ei mitä tahansa."""
    target = _write_variant(
        tmp_path, **{"pistol_rounds = [1, 13]": "pistol_rounds = [1, 16]"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "pistol_rounds" in str(exc.value)


def test_changing_mr_consistently_is_accepted(tmp_path: Path) -> None:
    """Ristiriitatarkistus ei estä liigaformaatin vaihtoa, kun kaikki päivittyy."""
    target = _write_variant(
        tmp_path,
        **{
            "mr = 12": "mr = 15",
            "regulation_rounds = 24": "regulation_rounds = 30",
            "pistol_rounds = [1, 13]": "pistol_rounds = [1, 16]",
        },
    )
    s = load_settings(target, env_files=())
    assert s.league.mr == 15
    assert s.thresholds.pistol_rounds == [1, 16]


def test_default_ban_outside_map_pool_is_rejected(tmp_path: Path) -> None:
    target = _write_variant(
        tmp_path,
        **{'own_default_bans = ["de_mirage", "de_dust2"]': 'own_default_bans = ["de_overpass"]'},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "own_default_bans" in str(exc.value)


# --- Rivi 6: avain puuttuu ---------------------------------------------------


def test_missing_api_key_tells_path_and_required_line(
    settings_file: Path, env_file
) -> None:
    """.env ilman FACEIT_API_KEY -> virhe kertoo polun ja tarvittavan rivin."""
    env = env_file(FACEIT_DOWNLOADS_TOKEN=FAKE_TOKEN)
    settings = _load(settings_file, env)

    assert settings.secret_status("FACEIT_API_KEY") == "puuttuu"
    with pytest.raises(SettingsError) as exc:
        settings.require_faceit_api_key()

    message = str(exc.value)
    assert "FACEIT_API_KEY" in message
    assert str(env) in message
    assert "FACEIT_API_KEY=" in message
    # Ei paljasta muita arvoja.
    assert FAKE_TOKEN not in message


def test_missing_env_file_falls_back_to_machine_path_in_message(
    settings_file: Path,
) -> None:
    """Ilman .env-tiedostoa virhe ohjaa koneen omaan avaintiedostoon."""
    settings = load_settings(settings_file, env_files=())
    with pytest.raises(SettingsError) as exc:
        settings.require_faceit_downloads_token()
    assert str(secrets_env_path()) in str(exc.value)


def test_present_key_is_returned_and_never_repr_ed(
    settings_file: Path, env_file
) -> None:
    env = env_file(FACEIT_API_KEY=FAKE_KEY, FACEIT_DOWNLOADS_TOKEN=FAKE_TOKEN)
    settings = _load(settings_file, env)

    assert settings.require_faceit_api_key() == FAKE_KEY
    assert settings.require_faceit_downloads_token() == FAKE_TOKEN
    assert settings.secret_status("FACEIT_API_KEY") == "asetettu"

    # Avain ei saa vuotaa lokiin, tulosteeseen eikä sarjallistukseen.
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
    assert FAKE_KEY not in str(settings.model_dump())
    assert FAKE_KEY not in settings.model_dump_json()


def test_blank_key_counts_as_missing(settings_file: Path, env_file) -> None:
    env = env_file(FACEIT_API_KEY="   ")
    settings = _load(settings_file, env)
    assert settings.secret_status("FACEIT_API_KEY") == "puuttuu"
    with pytest.raises(SettingsError):
        settings.require_faceit_api_key()


def test_machine_env_wins_over_project_env(settings_file: Path, tmp_path: Path) -> None:
    """Projektin .env on vain varalta; koneen oma tiedosto voittaa."""
    project_env = tmp_path / "project.env"
    project_env.write_text("FACEIT_API_KEY=vanha\n", encoding="utf-8")
    machine_env = tmp_path / "machine.env"
    machine_env.write_text(f"FACEIT_API_KEY={FAKE_KEY}\n", encoding="utf-8")

    settings = load_settings(settings_file, env_files=(project_env, machine_env))
    assert settings.require_faceit_api_key() == FAKE_KEY
    assert settings.secrets_file == machine_env


def test_secrets_path_follows_the_machine_home() -> None:
    """Avaintiedosto etsitään koneen kotihakemistosta, ei repon alta."""
    path = secrets_env_path()
    assert path.name == ".env"
    assert path.parent.name == ".pappascout"
    assert path.parent.parent == Path.home()


def test_settings_error_is_a_pappascout_error() -> None:
    assert issubclass(SettingsError, PappascoutError)


# --- settings.toml sisältö: jokainen luku jäljitettävissä --------------------


def test_sections_are_separate_typed_models(settings_file: Path) -> None:
    """AD-3: vaihe saa vain oman osansa, joten osiot ovat omia mallejaan."""
    s = _load(settings_file)
    assert isinstance(s.project, ProjectSettings)
    assert isinstance(s.league, LeagueSettings)
    assert isinstance(s.parse, ParseSettings)
    assert isinstance(s.thresholds, ThresholdSettings)
    assert isinstance(s.economy, EconomySettings)
    # Osa-asetus ei näe muita osioita.
    assert not hasattr(s.parse, "thresholds")
    assert not hasattr(s.thresholds, "parse")


def test_sections_are_frozen(settings_file: Path) -> None:
    """Asetuksia ei muuteta ajon aikana -- parametrihash pysyy totena."""
    s = _load(settings_file)
    with pytest.raises(ValidationError):
        s.thresholds.full_equip_min = 1  # type: ignore[misc]


def test_project_values(settings_file: Path) -> None:
    s = _load(settings_file)
    assert s.project.own_team_name == "PotkukelkkaPeek"
    assert s.project.language == "fi"
    assert s.project.lock_ttl_seconds == 600


def test_real_archive_root_is_portable() -> None:
    """Versioitu polku ei saa sisältää kovakoodattua käyttäjänimeä."""
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    line = next(r for r in text.splitlines() if r.startswith("archive_root"))
    assert "%USERPROFILE%" in line or "~" in line
    assert "vpu" not in line
    # Arkisto on OneDrivessa, koodi ei.
    assert "OneDrive" in line


def test_league_values_match_season_13(settings_file: Path) -> None:
    s = _load(settings_file)
    assert s.league.season == 13
    assert s.league.championship_ids == ["94681888-b5da-4ab5-bf50-f44b666b98a3"]
    assert s.league.organizer_id == "1bfc69fa-5a21-4ed9-9ef3-37edbd7210d8"
    assert s.league.map_pool == [
        "de_mirage",
        "de_inferno",
        "de_dust2",
        "de_nuke",
        "de_ancient",
        "de_anubis",
        "de_cache",
    ]
    assert "de_overpass" not in s.league.map_pool  # poistui Active Dutysta 7/2026
    assert s.league.own_default_bans == ["de_mirage", "de_dust2"]
    assert s.league.mr == 12
    assert s.league.ot_start_money == 12500


def test_parse_values(settings_file: Path) -> None:
    s = _load(settings_file)
    assert s.parse.snapshot_seconds == [6.0, 15.0, 30.0, 45.0]
    assert s.parse.first_contact_fallback_death is True
    assert "hegrenade" in s.parse.first_contact_exclude_weapons
    # Mitattu Ancient-demon 374 lentoradasta Story 2.2:ssa: rajan 500 sisällä
    # alue saadaan 178:lle, ja niistä 76 %:ssa kaikki rajan sisällä olevat
    # pelaajat ovat samalla alueella. Raja koskee vain räjähdystä -- heiton
    # alue luetaan heittäjältä itseltään. Arvo on asetus eikä koodia.
    assert s.parse.area_snap_units == 500


def test_threshold_values(settings_file: Path) -> None:
    s = _load(settings_file)
    t = s.thresholds
    assert t.pistol_rounds == [1, 13]
    assert t.regulation_rounds == 24
    # Kalibroitu 2026-08-29 kalibrointi-kierrostyypit.md:n totuustaulua vasten.
    # Nämä neljä ovat lukittuja: perustelu kullekin arvolle on settings.tomlin
    # kommentissa, ja arvon muuttaminen ilman tämän testin muuttamista
    # tarkoittaisi, että perustelu jäi lukematta.
    assert t.full_equip_min == 4000
    assert t.anomaly_equip_max_after_win == 2000
    assert t.force_buy_min == 1500
    assert t.force_money_left_max == 1000
    assert t.loss_count_half_start == 1
    assert t.loss_count_min == 0
    assert t.loss_count_max == 4
    assert t.team_identity_min_common == 3
    assert t.small_sample_rounds == 3


def test_economy_values(settings_file: Path) -> None:
    s = _load(settings_file)
    e = s.economy
    assert e.loss_bonus_steps == [1400, 1900, 2400, 2900, 3400]
    assert e.win_reward_elimination == 3250
    assert e.win_reward_bomb == 3500
    # CS2-arvo, ei CS:GO:n 800.
    assert e.plant_bonus_loss == 600
    assert e.ct_kill_bonus == 50
    # Ristiriita ratkaistu: M4A4 2900, ei 3100.
    assert e.prices["m4a4"] == 2900
    assert e.prices["ak47"] == 2700
    assert e.prices["mp9"] == 1250
    assert e.prices["mac10"] == 1050
    assert e.kill_rewards["awp"] == 100


def test_loss_bonus_step_matches_half_start(settings_file: Path) -> None:
    """Puoliaika alkaa loss countista 1 -> pistoolihäviö tuottaa 1900 $."""
    s = _load(settings_file)
    steps = s.economy.loss_bonus_steps
    assert steps[s.thresholds.loss_count_half_start] == 1900
    assert len(steps) == s.thresholds.loss_count_max + 1


def test_settings_env_var_pointing_nowhere_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ymparistomuuttuja on kasky, ei ehdotus.

    Hiljainen paluu tyohakemistoon lukisi eri asetukset kuin kayttaja pyysi --
    ja kalibroinnissa se tarkoittaisi, etta saadetty arvo ei vaikuta mihinkaan.
    """
    missing = tmp_path / "ei-ole.toml"
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(missing))
    with pytest.raises(SettingsError) as exc:
        load_settings()
    message = str(exc.value)
    assert SETTINGS_ENV_VAR in message
    assert str(missing) in message
