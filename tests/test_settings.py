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
from pappascout.constants import seconds_label
from pappascout.domain.models import (
    AggregateSettings,
    MAX_BUY_WINDOW_SECONDS,
    MAX_SNAPSHOT_SECONDS,
    REMOVED_SETTINGS,
    SETTINGS_ENV_VAR,
    SETTINGS_SECTIONS,
    EconomySettings,
    LeagueSettings,
    ParseSettings,
    ProjectSettings,
    ReportSettings,
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


def test_a_player_counter_above_the_roster_is_refused(tmp_path: Path) -> None:
    """P2: ilman tätä ``half`` on tavoittamaton eikä mikään huomauta.

    Puolioston molemmat ehdot ovat pelaajalaskureita. Jos kumpi tahansa
    vaatii enemmän pelaajia kuin kokoonpanossa on, ehto ei voi täyttyä
    yhdelläkään kierroksella -- ja jokainen hävityn jälkeinen ostos olisi
    force tai eco.
    """
    for name in ("armed_players_min", "normal_buy_players_min"):
        target = _write_variant(tmp_path, **{f"{name} = 3": f"{name} = 6"})
        with pytest.raises(SettingsError) as exc:
            load_settings(target)
        assert name in str(exc.value)
        # Vertailu on kentällä oleviin eikä rosterin kokoon (Story 2.5:n
        # katselmus): rosterissa voi olla vaihtopelaajia, kentällä ei.
        assert "kentällä olevien" in str(exc.value)


def test_a_threshold_below_the_smallest_loss_bonus_makes_force_unreachable(
    tmp_path: Path,
) -> None:
    """Ehto B menisi läpi ilman senttiäkään omaa rahaa.

    Poistunut ``force_money_left_max < force_buy_min`` varmisti, että
    puoliosto on saavutettavissa. Sen tilalle tarvitaan vartija molempiin
    suuntiin, ja tämä on toinen: jos kynnys on enintään pienin häviöbonus,
    jokainen pelaaja täyttää ehdon B aina eikä yksikään hävityn jälkeinen
    ostos voi enää olla force.

    Raja on kahden osion välissä (``[thresholds]`` ja ``[economy]``), joten
    kumpikaan ei voi tarkistaa sitä yksin.
    """
    target = _write_variant(
        tmp_path,
        **{"normal_buy_money_min = 4000": "normal_buy_money_min = 1400"},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    message = str(exc.value)
    assert "normal_buy_money_min" in message
    assert "force" in message


def test_a_threshold_above_the_money_ceiling_makes_a_half_buy_unreachable(
    tmp_path: Path,
) -> None:
    """Sama vartija toiseen suuntaan: kukaan ei voi koskaan täyttää ehtoa B.

    Ostovoima katkaistaan rahakattoon, joten kattoa suurempi kynnys on
    saavuttamaton -- ja puoliosto katoaisi äänettömästi kokonaan.
    """
    target = _write_variant(
        tmp_path,
        **{"normal_buy_money_min = 4000": "normal_buy_money_min = 20000"},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    message = str(exc.value)
    assert "normal_buy_money_min" in message
    assert "max_money" in message


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
        **{"force_buy_min = 1500": "force_buy_min = 1500\nforce_money_max = 2500"},
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
    # Kalibroitu Story 2.9:ssä kaikilla kuudella demolla (2 544 räjähdystä):
    # rajan 256 sisään osuu 95,4 %. Raja koskee vain räjähdystä -- heiton alue
    # luetaan heittäjältä itseltään. Arvo on asetus eikä koodia.
    assert s.parse.area_snap_units == 256
    # Pistepilven kolme mittaa. Ne ovat asetuksia eivätkä koodia, ja niiden
    # arvot on mitattu (ks. settings.tomlin taulukot). Paino on 1 eikä 2:
    # mitattuna 1 erottaa kerrokset täydellisesti ja kattaa enemmän.
    assert s.parse.callout_grid_units == 32
    assert s.parse.callout_z_weight == 1.0
    assert s.parse.callout_z_tolerance_units == 72.0


def test_armed_counter_has_no_setting(settings_file: Path) -> None:
    """Kalustolaskurilla ei ole kynnysasetusta missään osiossa.

    Story 1.5:n ``armed_player_equip_min`` mittasi varustearvoa, joka on ase
    + panssari + kranaatit yhtenä lukuna. Story 1.6 vaihtoi mittarin
    havaintoon "panssari ja vähintään yksi ostettu ase", eikä havainnolla ole
    kynnystä. Jäänyt asetus olisi pahempi kuin poistettu: ``extra="forbid"``
    kaataisi latauksen, mutta vain jos avain on tiedostossa -- tämä testi
    kaatuu myös silloin, kun avain palaa koodiin.
    """
    s = _load(settings_file)
    for section in (ParseSettings, ThresholdSettings, EconomySettings):
        assert "armed_player_equip_min" not in section.model_fields
    for loaded in (s.parse, s.thresholds, s.economy):
        assert not hasattr(loaded, "armed_player_equip_min")


def test_settings_file_has_no_armed_threshold(settings_file: Path) -> None:
    """Avainta ei ole enää asetettu ``settings.toml``issa.

    ``extra="forbid"`` kaataisi latauksen, joten tämä on käytännössä
    ``_load``in toistoa -- mutta se nimeää syyn: jäänyt rivi olisi
    tuotannon asetustiedostossa, ei testin muunnelmassa. Kommenteissa nimi
    saa esiintyä: siellä se kertoo, mikä poistui ja miksi.
    """
    lines = settings_file.read_text(encoding="utf-8").splitlines()
    assigned = [
        line
        for line in lines
        if not line.lstrip().startswith("#")
        and "armed_player_equip_min" in line
    ]
    assert assigned == []


def test_old_settings_file_gets_a_migration_message(tmp_path: Path) -> None:
    """Vanha ``settings.toml`` kertoo mitä tilalle tuli, ei vain "ei kelpaa".

    ``extra="forbid"`` hylkää tiedoston joka tapauksessa, mutta geneerisellä
    "tuntematon avain" -viestillä: käyttäjä näkisi vain, ettei hänen
    tiedostonsa kelpaa. Kahden koneen arkistossa toisella koneella on
    tavallisesti vanha tiedosto, joten tämä on odotettu tilanne eikä
    poikkeus -- ja käyttäjä ei koodaa itse.
    """
    target = _write_variant(
        tmp_path,
        **{"area_snap_units = 256": "area_snap_units = 256\narmed_player_equip_min = 950"},
    )
    with pytest.raises(SettingsError) as exc:
        _load(target)

    message = str(exc.value)
    assert "armed_player_equip_min" in message
    # Nimeää sekä osion että sen, mitä tilalle tuli ja mitä tehdä.
    assert "[parse]" in message
    assert "panssari" in message
    assert "Poista rivi" in message


def test_the_retired_money_left_threshold_names_its_three_replacements(
    tmp_path: Path,
) -> None:
    """Story 1.10 poisti ``force_money_left_max``: ohje kertoo mitä tilalle.

    Kolme uutta kynnystä yhden tilalle on juuri se muutos, jota käyttäjä ei
    voi arvata. Ilman ohjetta hän näkisi vain "tuntematon avain".
    """
    target = _write_variant(
        tmp_path,
        **{"force_buy_min = 1500": "force_buy_min = 1500\nforce_money_left_max = 1000"},
    )
    with pytest.raises(SettingsError) as exc:
        _load(target)

    message = str(exc.value)
    assert "force_money_left_max" in message
    assert "[thresholds]" in message
    for replacement in (
        "normal_buy_money_min",
        "normal_buy_players_min",
        "armed_players_min",
    ):
        assert replacement in message


def test_every_removed_setting_has_an_instruction() -> None:
    """Jokainen poistettu asetus kertoo mitä tehdä, ei vain että se poistui.

    Tyhjä tai ympäripyöreä ohje olisi sama kuin ei ohjetta lainkaan.
    """
    assert REMOVED_SETTINGS
    for (section, key), advice in REMOVED_SETTINGS.items():
        assert section in SETTINGS_SECTIONS, section
        assert key and advice.strip(), key
        assert len(advice) > 60, key


def test_adapter_needs_no_armed_setting() -> None:
    """Adapterin voi rakentaa ilman kalustolaskurin parametreja.

    Sääntö ja aseluettelo ovat koodia (``pappascout.constants``), joten
    adapterilla ei ole oletusta, joka voisi erkaantua asetuksesta.
    """
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    adapter = Demoparser2Adapter()
    assert not hasattr(adapter, "armed_player_equip_min")


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
    # Puolioston kaksi ehtoa (Story 1.10). Ehto A on Veetin lausuma raja,
    # ehto B kalibroitu inferno_vs_ryhmaraman kierroksia 6 ja 10 vasten.
    assert t.armed_players_min == 3
    assert t.normal_buy_money_min == 4000
    assert t.normal_buy_players_min == 3
    assert t.loss_count_half_start == 1
    assert t.loss_count_min == 0
    assert t.loss_count_max == 4
    assert t.team_identity_min_common == 3
    assert t.small_sample_rounds == 3
    # Poikkeamakynnykset (Story 2.5). Nämä kuusi ovat lukittuja samalla
    # perusteella kuin kierrostyyppien kynnykset: jokainen on mitattu
    # kahdeksalla demolla ja kahdella joukkueella
    # (``kalibrointi-ct-eteneminen.md``), ja arvon muuttaminen ilman tämän
    # testin muuttamista tarkoittaisi, että perustelu jäi lukematta.
    assert t.advance_t_share == 0.80
    assert t.advance_area_min_observations == 20
    assert t.advance_max_sample_s == 30.0
    assert t.advance_min_players == 1
    assert t.crunch_min_players == 2
    assert t.crunch_min_sources == 2
    # Stackin kolme kynnystä (Story 2.14), sama peruste: jokainen on mitattu
    # kahdeksalla demolla (``kalibrointi-stack.md``). Neljä on kalibroitu ja
    # kolme ei; 1,25 tuottaa Ancientilla saman aluejaon kaikista kolmesta
    # demosta; 2,0 erottaa Nuken (0,47-0,54) muista kartoista (3,70-5,04).
    assert t.stack_min_players == 4
    assert t.stack_group_margin == 1.25
    assert t.stack_site_separation_min == 2.0


@pytest.mark.parametrize(
    "value",
    [
        # Alaraja on **enemmistö**: 0,5 ja alle tekisi alueesta molempien.
        -0.01,
        0.0,
        0.5,
        # Yläraja on osuuden määritelmä.
        1.01,
        # NaN tekisi jokaisesta vertailusta epätoden, ääretön päinvastoin.
        float("nan"),
        float("inf"),
    ],
)
def test_an_impossible_t_share_is_refused(value: float) -> None:
    """Kynnys kertoo kummalle puolelle alue **kuuluu**, ei kummalla se käy."""
    with pytest.raises(ValidationError):
        ThresholdSettings(pistol_rounds=[1, 13], advance_t_share=value)


def test_the_t_share_bound_itself_is_allowed() -> None:
    """Vartijan toinen haara: juuri yli puolet kelpaa."""
    assert (
        ThresholdSettings(pistol_rounds=[1, 13], advance_t_share=0.51).advance_t_share
        == 0.51
    )


def test_a_crunch_looser_than_an_advance_is_allowed() -> None:
    """**Crunch ei ole etenemisen tiukempi muoto**, joten järjestystä ei ole.

    Katselmuskierros 1 poisti vartijan, joka vaati
    ``crunch_min_players >= advance_min_players``. Perustelu oli epätosi:
    crunch on tiukempi suunnista ja löysempi kierrostyypistä, joten kumpikaan
    osumajoukko ei sisällä toista (mitattu: MatureMayhem Anubis k10 on täysi
    osto, jolla etenemistä ei ole olemassa). Testi on tässä siksi, ettei
    vartija palaisi vahingossa.
    """
    limits = ThresholdSettings(
        pistol_rounds=[1, 13],
        advance_min_players=3,
        crunch_min_players=2,
    )
    assert (limits.advance_min_players, limits.crunch_min_players) == (3, 2)


def test_more_sources_than_players_is_refused() -> None:
    """Jokainen lähtösuunta tarvitsee oman pelaajansa."""
    with pytest.raises(ValidationError, match="Jokainen lähtösuunta"):
        ThresholdSettings(
            pistol_rounds=[1, 13],
            crunch_min_players=2,
            crunch_min_sources=3,
        )


def test_a_single_crunch_source_is_refused() -> None:
    """Crunch on määritelmällisesti saapumista **useasta** suunnasta.

    Arvolla 1 sekä sääntö että raportin lukuohje väittäisivät jotain, mitä
    koodi ei enää vaadi.
    """
    with pytest.raises(ValidationError):
        ThresholdSettings(pistol_rounds=[1, 13], crunch_min_sources=1)


@pytest.mark.parametrize(
    "key",
    ["advance_min_players", "crunch_min_players", "stack_min_players"],
)
def test_an_anomaly_player_minimum_above_the_server_is_refused(key: str) -> None:
    """Kuusi pelaajaa alueella ei ole tiukempi kynnys vaan mahdoton ehto.

    Vertailu on **kentällä oleviin** eikä rosterin kokoon: rosterissa voi olla
    vaihtopelaajia (mitattu: seitsemän yhdellä joukkueella), mutta kentällä on
    aina viisi.
    """
    with pytest.raises(ValidationError, match="kentällä olevien"):
        ThresholdSettings(pistol_rounds=[1, 13], **{key: 6})


def test_five_defenders_is_a_valid_stack_threshold() -> None:
    """Viisi on säännön aito ääripää eikä mahdoton ehto.

    Mitattuna ``stack_min_players = 5`` antaa 2 kierrosta 66:sta, joten
    vartija ei saa hylätä sitä -- kuusi puolustajaa on eri asia.
    """
    limits = ThresholdSettings(pistol_rounds=[1, 13], stack_min_players=5)
    assert limits.stack_min_players == 5


@pytest.mark.parametrize(
    "value",
    [
        # Alaraja on 1,0: sitä pienemmällä "lähempi" site voisi olla kauempana.
        0.99,
        0.0,
        -1.0,
        # Yläraja torjuu arvon, jolla jokainen muu alue putoaisi ryhmättömäksi.
        10.01,
        float("inf"),
        float("nan"),
    ],
)
def test_an_impossible_group_margin_is_refused(value: float) -> None:
    """Marginaalin rajat ovat **mallissa** eivätkä vain säännössä.

    Sama ehto on kirjoitettu kahdesti (pydantic ja ``site_groups``in
    ValueError), koska sääntö on julkinen funktio eikä näe asetuksia. Ilman
    tätä testiä vain domain-kopio olisi todistettu -- ja asetustiedostosta
    tuleva arvo menisi läpi.
    """
    with pytest.raises(ValidationError):
        ThresholdSettings(pistol_rounds=[1, 13], stack_group_margin=value)


@pytest.mark.parametrize(
    "value",
    [
        # Alaraja on yli 0: nollalla vartija ei vaientaisi yhtäkään karttaa.
        0.0,
        -1.0,
        # Yläraja: mitattu maksimi on 5,04, joten yli 20 vaientaisi kaikki.
        20.01,
        float("inf"),
        float("nan"),
    ],
)
def test_an_impossible_site_separation_is_refused(value: float) -> None:
    """Erottuvuuskynnyksellä on katto samasta syystä kuin marginaalilla.

    Kynnyksen nostaminen ei tiukenna sääntöä vaan hiljentää sen: liian
    suurella arvolla jokainen kartta vaikenee, ja raportti väittäisi "ei
    stackeja" havaintona vaikka yhtäkään demoa ei tutkittu.
    """
    with pytest.raises(ValidationError):
        ThresholdSettings(
            pistol_rounds=[1, 13], stack_site_separation_min=value
        )


def test_the_measured_stack_thresholds_are_inside_their_bounds() -> None:
    """Vartijan toinen suunta: mitatut arvot kelpaavat."""
    limits = ThresholdSettings(
        pistol_rounds=[1, 13],
        stack_group_margin=1.0,
        stack_site_separation_min=20.0,
    )
    assert (limits.stack_group_margin, limits.stack_site_separation_min) == (
        1.0,
        20.0,
    )


def test_a_player_minimum_is_measured_against_the_server_not_the_roster() -> None:
    """Seitsemän pelaajan rosteri ei tee kuudesta aseistetusta mahdollista."""
    with pytest.raises(ValidationError, match="kentällä olevien"):
        ThresholdSettings(
            pistol_rounds=[1, 13], roster_size=7, armed_players_min=6
        )


@pytest.mark.parametrize(
    "value",
    [
        # Nolla vaientaisi molemmat säännöt pysyvästi.
        0.0,
        -1.0,
        # Yläraja: aikaraja valikoi näytepisteistä, joten sitä suurempi arvo
        # ei rajaa mitään -- ja 300 on kirjoitusvirhe.
        61.0,
        300.0,
        float("nan"),
        float("inf"),
    ],
)
def test_an_impossible_anomaly_time_bound_is_refused(value: float) -> None:
    with pytest.raises(ValidationError):
        ThresholdSettings(pistol_rounds=[1, 13], advance_max_sample_s=value)


def test_a_time_bound_below_the_first_sample_point_is_refused(
    tmp_path: Path,
) -> None:
    """Ristiintarkistus osioiden välillä: raja vaientaisi molemmat säännöt.

    Raportti väittäisi silloin "ei poikkeamia" havaintona, vaikka yhtäkään
    näytepistettä ei koskaan tutkittu. Kumpikaan osio ei voi tarkistaa tätä
    yksin, joten tarkistus on ``Settings._check_sections_agree``issa.
    """
    target = _write_variant(
        tmp_path,
        **{"advance_max_sample_s = 30.0": "advance_max_sample_s = 3.0"},
    )
    with pytest.raises(SettingsError, match="varhaisin parse.snapshot_seconds"):
        load_settings(target)


def test_a_time_bound_at_the_first_sample_point_is_allowed(
    tmp_path: Path,
) -> None:
    """Vartijan toinen haara: tasan varhaisin näytepiste kelpaa."""
    target = _write_variant(
        tmp_path,
        **{"advance_max_sample_s = 30.0": "advance_max_sample_s = 6.0"},
    )
    assert load_settings(target).thresholds.advance_max_sample_s == 6.0


@pytest.mark.parametrize(
    "value,message",
    [
        ([10.0, 5.0], "aidosti kasvava"),
        ([5.0, 5.0], "aidosti kasvava"),
        ([0.0], "ei ole positiivinen"),
        ([-1.0], "ei ole positiivinen"),
        ([float("nan")], "äärellinen"),
        ([float("inf")], "äärellinen"),
        # Kaksi rajaa, jotka näyttävät samalta lokeron nimessä.
        ([5.000000001, 5.000000002], "samalta lokeron nimessä"),
    ],
)
def test_utility_seconds_buckets_are_checked_at_load(
    value: list[float], message: str
) -> None:
    """Järjestämätön tai mahdoton raja jäisi muuten hiljaa tyhjäksi lokeroksi."""
    with pytest.raises(ValidationError, match=message):
        AggregateSettings(utility_seconds_buckets=value)


def test_utility_seconds_buckets_may_be_empty() -> None:
    """Aikaikkunan poistaminen on kelvollinen valinta, ei koodimuutos."""
    a = AggregateSettings(utility_seconds_buckets=[])
    assert a.utility_seconds_buckets == []


def test_aggregate_section_is_read_from_the_settings_file(
    settings_file: Path,
) -> None:
    """``[aggregate]`` on oma osionsa, jotta classify ei hashaa sitä."""
    s = _load(settings_file)
    assert s.aggregate.utility_seconds_buckets == [5.0, 10.0, 20.0]
    assert not hasattr(s.thresholds, "utility_seconds_buckets")


def test_aggregate_default_matches_the_settings_file() -> None:
    """Koodioletus ei saa erota asetustiedostosta.

    Tyhjä oletus tuottaisi hiljaa yhden lokeron raportin, jos avain unohtuisi
    tiedostosta -- eikä mikään kertoisi että aikaikkunat katosivat.
    """
    assert AggregateSettings().utility_seconds_buckets == [5.0, 10.0, 20.0]


# --- Karsintasäännöt (Story 2.13) ---------------------------------------------


def test_report_section_is_read_from_the_settings_file(
    settings_file: Path,
) -> None:
    """``[report]`` on oma osionsa, jotta aggregate ei hashaa sitä.

    Osiointi seuraa vaihetta, joka arvot lukee (AD-3), ja nämä lukee
    ``render``. ``[aggregate]``iin kirjoitettuna karsintasäännön säätäminen
    mitätöisi jokaisen aggregoinnin -- eli esitysvalinta pakottaisi laskemaan
    report.jsonin uudelleen, vaikka sen sisältö ei muutu.
    """
    s = _load(settings_file)
    assert s.report.drop_saturated_equipment_lines is True
    assert s.report.merge_equal_equipment_lines is True
    assert s.report.skip_sample_seconds == []
    assert s.report.max_utility_targets == 2
    assert s.report.max_kill_areas == 3
    assert not hasattr(s.aggregate, "max_kill_areas")
    assert not hasattr(s.thresholds, "max_kill_areas")
    assert "report" in SETTINGS_SECTIONS


def test_report_defaults_match_the_settings_file(settings_file: Path) -> None:
    """Koodioletus ei saa erota asetustiedostosta.

    Karsinta on oletuksena päällä neljällä säännöllä ja pois yhdellä. Jos
    koodioletus eroaisi tiedostosta, unohtunut avain karsisi eri tavalla kuin
    tiedosto sanoo -- eikä mikään kertoisi, kummasta raportti syntyi.
    """
    assert ReportSettings() == _load(settings_file).report


def test_the_late_sample_point_is_off_by_default() -> None:
    """Sääntö 3 on mittaustulos: 45 s ei ole toistoa vaan ohut havainto.

    Mitattu kaikista kahdeksasta demosta: 45 s -piste kuvaa 53 % joukkueesta
    ja on olemassa 285/354 kierrospuolella. Se on vinoutunut, mutta Veetin
    analyyseissä on myöhäisen kierroksen havaintoja, joten poistaminen voi
    maksaa sisältöä -- asetus on olemassa, oletus on säilyttää.
    """
    assert ReportSettings().skip_sample_seconds == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([float("nan")], "äärellinen"),
        ([float("inf")], "äärellinen"),
        ([0.0], "positiivinen"),
        ([-45.0], "positiivinen"),
        ([45.0, 45.0], "kahdesti"),
        ([45.000000001, 45.000000002], "kahdesti"),
    ],
)
def test_skip_sample_seconds_are_checked_at_load(
    value: list[float], message: str
) -> None:
    """Arvo, joka ei voi täsmätä näytepisteeseen, on kirjoitusvirhe.

    Ilman tarkistusta asetus näyttäisi poistavan rivin muttei poistaisi
    mitään, ja sen huomaaminen raportista on vaikeaa: rivi on siellä missä se
    olikin.
    """
    with pytest.raises(ValidationError, match=message):
        ReportSettings(skip_sample_seconds=value)


def test_a_sample_point_outside_the_parse_setting_is_allowed() -> None:
    """Osiot eivät näe toisiaan (AD-3), eikä tämä ole ristiriita.

    Raportti latotaan myös vanhoista report.jsoneista, joiden näytepisteet
    ovat ne, jotka parsinnan aikaan olivat käytössä. Torjunta vaatisi, että
    ``[report]`` tuntee ``[parse]``in -- ja tekisi vanhan aggregoinnin
    renderöimisestä virheen.
    """
    assert ReportSettings(skip_sample_seconds=[7.5]).skip_sample_seconds == [7.5]


@pytest.mark.parametrize("key", ["max_utility_targets", "max_kill_areas"])
def test_a_negative_pruning_limit_is_refused(key: str) -> None:
    """``0`` on "ei rajaa"; negatiivinen ei tarkoita mitään."""
    with pytest.raises(ValidationError):
        ReportSettings(**{key: -1})


@pytest.mark.parametrize("key", ["max_utility_targets", "max_kill_areas"])
def test_a_zero_pruning_limit_means_no_limit(key: str) -> None:
    """Säännön poiskääntäminen on kelvollinen valinta, ei koodimuutos."""
    assert getattr(ReportSettings(**{key: 0}), key) == 0


def test_the_sample_point_list_is_ordered_at_load(tmp_path: Path) -> None:
    """Kohta G1: järjestys ei saa muuttaa parametrihashia.

    ``render`` hashaa osionsa kokonaisena, ja ``[45, 15]`` tuottaa merkki
    merkiltä saman raportin kuin ``[15, 45]``. Järjestämättömänä manifesti
    väittäisi kahden identtisen raportin syntyneen eri parametreilla -- eli
    kertoisi erosta, jota ei ole.
    """
    assert ReportSettings(skip_sample_seconds=[45.0, 15.0]).skip_sample_seconds == [
        15.0,
        45.0,
    ]
    target = _write_variant(
        tmp_path, **{"skip_sample_seconds = []": "skip_sample_seconds = [45.0, 15.0]"}
    )
    assert _load(target).report.skip_sample_seconds == [15.0, 45.0]


def test_the_validator_and_the_report_share_one_seconds_format() -> None:
    """Kohta H9: kaksi kerrosta, yksi muotoilu.

    Latausvaiheen tarkistus "kaksi arvoa näyttäisi rivillä samalta" ja rivin
    nimiö ovat sama funktio (``constants.seconds_label``). Kahtena kopiona ne
    sopisivat vain tänään: yhden desimaalin lisääminen riville tekisi
    kahdesta asetusarvosta saman rivin ilman että validointi huomaisi.

    Virheteksti tulostaa saman muodon kuin raportti, eli desimaalipilkun.
    """
    assert seconds_label(45.5) == "45,5"
    with pytest.raises(ValidationError, match="45,5"):
        ReportSettings(skip_sample_seconds=[-45.5])
    with pytest.raises(ValidationError, match="kahdesti"):
        ReportSettings(skip_sample_seconds=[45.0, 45.0000001])


def test_a_missing_section_is_a_finnish_error_that_says_what_to_do(
    tmp_path: Path,
) -> None:
    """Kohta G2: ``report: Field required`` ei ohjaa mihinkään.

    Osion pakollisuus on projektin linjaus eikä puute -- jokainen asetus
    kirjoitetaan näkyviin -- joten korjattava on **viesti**. Kahden koneen
    arkisto tekee tästä tavallisen tilanteen: repon ``settings.toml``
    päivittyy gitistä, ja väliin jäänyt pull näkyy juuri näin.
    """
    text = settings_text(tmp_path / "arkisto")
    lines = text.splitlines(keepends=True)
    kept, skip = [], False
    for line in lines:
        if line.startswith("[report]"):
            skip = True
        elif line.startswith("[economy]"):
            skip = False
        if not skip:
            kept.append(line)
    target = tmp_path / "ilman-reporttia.toml"
    target.write_text("".join(kept), encoding="utf-8")

    with pytest.raises(SettingsError) as exc:
        _load(target)
    message = str(exc.value)
    assert "[report]" in message
    assert "puuttuu osio" in message
    assert "git show HEAD:settings.toml" in message
    assert "Field required" not in message


def test_a_misspelled_pruning_rule_is_not_silently_ignored(
    tmp_path: Path,
) -> None:
    """Kirjoitusvirhe asetuksen nimessä ei saa jättää sääntöä päälle hiljaa."""
    target = _write_variant(tmp_path, **{"max_kill_areas = 3": "max_kill_area = 3"})
    with pytest.raises(SettingsError, match="max_kill_area"):
        _load(target)


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


# --- Ostoikkuna (Story 1.9) ---------------------------------------------------


def test_buy_window_is_the_games_own_rule(settings_file: Path) -> None:
    """Ostoikkuna on 20 s, eli CS2:n oma ostoaika kierroksen alusta.

    Arvo on **linjaus, jonka mittaus tukee**, ei kalibroitu kynnys, joten se
    ei saa liukua aineiston mukana. Viidestä liigademosta (106 kierrosta)
    mitattuna ostaminen loppui viimeistään 19,4 s kohdalla, mikä on
    yhteensopiva 20 sekunnin ostoajan kanssa -- mutta 19,4 ei ole se luku,
    joka tänne kuuluu, eikä mittaus voi kertoa mitä 20,5 s kohdalla olisi
    tapahtunut.
    """
    s = _load(settings_file)
    assert s.parse.buy_window_seconds == 20.0


def test_a_negative_buy_window_is_refused(tmp_path: Path) -> None:
    """Negatiivinen ikkuna siirtäisi mittauspisteen freezetimen sisään.

    Silloin talous luettaisiin hetkestä, jolloin joukkue ei ole vielä edes
    ostanut, eikä mikään kaatuisi -- luvut olisivat vain hiljaa liian pienet.
    """
    path = _write_variant(
        tmp_path, **{"buy_window_seconds = 20.0": "buy_window_seconds = -1.0"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "buy_window_seconds" in str(exc.value)


def test_a_buy_window_longer_than_a_round_is_refused(tmp_path: Path) -> None:
    """Kierrosta pidempi ikkuna on kirjoitusvirhe, ei valinta.

    Mittauspiste rajautuu joka tapauksessa kierroksen loppuun, joten arvo ei
    mittaisi mitään uutta -- se vain näyttäisi asetustiedostossa siltä, että
    ostoaika kestää koko kierroksen.
    """
    path = _write_variant(
        tmp_path, **{"buy_window_seconds = 20.0": "buy_window_seconds = 200.0"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "buy_window_seconds" in str(exc.value)


def test_a_zero_buy_window_is_allowed(tmp_path: Path) -> None:
    """Nolla on kelvollinen: se tarkoittaa "mittaa freezetimen lopusta".

    Se on Story 1.9:ää edeltävä käyttäytyminen ja ainoa tapa toistaa vanha
    mittaus ilman koodimuutosta.
    """
    path = _write_variant(
        tmp_path, **{"buy_window_seconds = 20.0": "buy_window_seconds = 0.0"}
    )
    assert _load(path).parse.buy_window_seconds == 0.0


@pytest.mark.parametrize("literal", ["nan", "inf", "-inf"])
def test_a_non_finite_buy_window_is_refused(tmp_path: Path, literal: str) -> None:
    """``nan`` ja ääretön eivät saa mennä läpi vertailujen välistä.

    TOML osaa nämä literaalit, ja ``nan`` läpäisee jokaisen vertailun:
    ``nan < 0`` on epätosi ja ``nan > raja`` on epätosi. Pelkistä vertailuista
    koostuva tarkistus päästäisi arvon läpi, ja ``round(nan * tick_rate)``
    kaatuisi vasta parsinnan sisällä -- sen jälkeen kun 400 MB:n demo on jo
    purettu ja luettu. Ääretön kaatuisi samassa kohdassa.
    """
    path = _write_variant(
        tmp_path,
        **{"buy_window_seconds = 20.0": f"buy_window_seconds = {literal}"},
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    message = str(exc.value)
    assert "buy_window_seconds" in message
    assert "äärellinen" in message


def test_the_buy_window_bound_is_its_own_not_the_snapshot_bound() -> None:
    """Ostoikkunan yläraja on oma vakionsa, ei näytepisteiden raja.

    Yhteinen vakio kytkisi kaksi riippumatonta asetusta toisiinsa: kumman
    tahansa säätäminen siirtäisi toisen rajaa huomaamatta. Näytepisteiden raja
    on kierroksen kesto (115 s), jonka alta menisi läpi esimerkiksi 100 s --
    arvo, jolla mittauspiste ei enää olisi ostoaika vaan mielivaltainen hetki
    kierroksen keskellä.
    """
    assert MAX_BUY_WINDOW_SECONDS < MAX_SNAPSHOT_SECONDS


def test_a_window_below_the_round_length_but_above_the_bound_is_refused(
    tmp_path: Path,
) -> None:
    """100 s on kierrosta lyhyempi mutta silti liikaa -- eikä mene äänettömästi."""
    path = _write_variant(
        tmp_path, **{"buy_window_seconds = 20.0": "buy_window_seconds = 100.0"}
    )
    assert 100.0 < MAX_SNAPSHOT_SECONDS
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "buy_window_seconds" in str(exc.value)


# --- Pistepilven mitat (Story 2.9) -----------------------------------------


@pytest.mark.parametrize("literal", ["0", "-32"])
def test_a_non_positive_grid_size_is_refused(tmp_path: Path, literal: str) -> None:
    """Nollan kokoinen ruutu ei ole ruudukko vaan nollalla jako.

    Ruudun indeksi on ``floor(x / särmä)``, joten nolla kaatuisi kesken
    400 MB:n demon parsinnan -- ja negatiivinen kääntäisi ruudukon nurin
    ilman että mikään kaatuisi.
    """
    path = _write_variant(
        tmp_path, **{"callout_grid_units = 32": f"callout_grid_units = {literal}"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "callout_grid_units" in str(exc.value)


@pytest.mark.parametrize(
    "key", ["callout_z_weight", "callout_z_tolerance_units"]
)
def test_a_negative_weighting_parameter_is_refused(tmp_path: Path, key: str) -> None:
    """Negatiivinen paino tai toleranssi kääntäisi etäisyyden nurin.

    Painotus on ``max(0, |dz| - toleranssi) * paino``: negatiivinen paino
    tekisi pystyerosta palkinnon, ja negatiivinen toleranssi rankaisisi
    pystyeroa, jota ei ole. Kumpikaan ei kaatuisi -- alue olisi vain hiljaa
    väärä.
    """
    current = {"callout_z_weight": "1.0", "callout_z_tolerance_units": "72"}[key]
    path = _write_variant(
        tmp_path, **{f"{key} = {current}": f"{key} = -1.0"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert key in str(exc.value)


@pytest.mark.parametrize(
    "key", ["callout_z_weight", "callout_z_tolerance_units"]
)
@pytest.mark.parametrize("literal", ["nan", "inf"])
def test_a_non_finite_weighting_parameter_is_refused(
    tmp_path: Path, key: str, literal: str
) -> None:
    """``nan`` ja ääretön eivät saa mennä läpi vertailujen välistä.

    Etäisyys kerrotaan painolla ja verrataan kynnykseen. ``nan`` tekisi
    jokaisesta vertailusta epätoden, joten **yksikään** räjähdys ei saisi
    aluetta -- ja tulos näyttäisi täsmälleen samalta kuin demo, jossa
    pistepilvi jäi tyhjäksi. Ääretön tekisi saman toisin päin.
    """
    current = {"callout_z_weight": "1.0", "callout_z_tolerance_units": "72"}[key]
    path = _write_variant(tmp_path, **{f"{key} = {current}": f"{key} = {literal}"})
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert key in str(exc.value)


def test_the_threshold_is_a_required_setting(tmp_path: Path) -> None:
    """Kynnys ei ole valinnainen: ilman sitä kattavuus olisi aina 100 %.

    Pistepilvestä lähin ruutu löytyy AINA, joten kynnyksetön ajo antaisi
    jokaiselle räjähdykselle alueen etäisyydestä riippumatta. Speksin
    Always-sääntö on "etäisyyskynnys säilyy", ja pakollinen kenttä tekee
    siitä rakenteellisen -- ei asian, jonka poistettu rivi kumoaisi
    hiljaa.
    """
    text = settings_text(tmp_path / "arkisto")
    without = "\n".join(
        line for line in text.splitlines()
        if not line.startswith("area_snap_units")
    )
    target = tmp_path / "ilman.toml"
    target.write_text(without, encoding="utf-8")
    with pytest.raises(SettingsError) as exc:
        _load(target)
    assert "area_snap_units" in str(exc.value)


@pytest.mark.parametrize("literal", ["1", "4", "2048"])
def test_a_grid_size_outside_the_bounds_is_refused(
    tmp_path: Path, literal: str
) -> None:
    """Rajat ovat suorituskykyä, eivät makuasia.

    Lähimmän haku on ristiintulo pisteiden ja ruutujen välillä: särmä 1
    tuottaisi luokkaa miljoona ruutua yhdestä demosta. Yläraja taas on se
    piste, jossa ruudukko ei enää erottele kartan alueita.
    """
    path = _write_variant(
        tmp_path, **{"callout_grid_units = 32": f"callout_grid_units = {literal}"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "callout_grid_units" in str(exc.value)


def test_the_grid_bounds_themselves_are_allowed(tmp_path: Path) -> None:
    """Rajat ovat mukaan lukevia: 8 ja 1024 kelpaavat, 4 ja 2048 eivät."""
    for literal in ("8", "1024"):
        path = _write_variant(
            tmp_path,
            **{"callout_grid_units = 32": f"callout_grid_units = {literal}"},
        )
        assert _load(path).parse.callout_grid_units == int(literal)


def test_a_zero_z_weight_is_allowed(tmp_path: Path) -> None:
    """Nolla on kelvollinen valinta: se tarkoittaa "älä katso korkeutta".

    Litteällä kartalla se on oikea vastaus, ja se on ainoa tapa toistaa
    painoton mittaus ilman koodimuutosta.
    """
    path = _write_variant(
        tmp_path, **{"callout_z_weight = 1.0": "callout_z_weight = 0.0"}
    )
    assert _load(path).parse.callout_z_weight == 0.0


def test_the_shipped_settings_keep_demos_in_the_archive(tmp_path: Path) -> None:
    """Demot menevät arkistoon, ja se on päätös eikä puuttuva arvo.

    Päätetty 2026-09-05 uuden tiedon jälkeen. Kaksi perustetta:

    1. **Arkisto seuraa koneelta toiselle**, koska se on OneDrivessa.
       Paikallisessa kansiossa olevat demot eivät seuraa, ja toisella koneella
       ne haettaisiin FACEITista uudelleen -- mikä onnistuu vain noin 30 päivän
       ajan.
    2. **Files On-Demand vapauttaa parsitun demon tilan poistamatta
       tiedostoa.** Paikallisessa kansiossa tilan vapauttaminen on lopullinen
       poisto.

    Väite kohdistuu **versioituun asetustiedostoon**, ja se on sama kohta, jossa
    päätös eläisi jos se kumottaisiin: rivin poistaminen kommenteista kääntää
    moodin, ja silloin tämä testi kertoo siitä.
    """
    from pappascout.archive.paths import ArchivePaths

    settings = load_settings(REAL_SETTINGS)
    assert settings.project.demos_root is None

    archive = ArchivePaths.from_settings(
        settings.project.archive_root, settings.project.demos_root
    )
    assert archive.demos_dir() == archive.root / "demos"
    assert archive.demos_dir() == archive.archive_demos_dir()


def test_the_demos_root_setting_stays_documented_in_the_shipped_file() -> None:
    """Kommentoitu rivi on ohje, ei jäänne.

    Asetus on tuettu moodi levytilan loppuessa koneella, jolla pilvi ei ole
    vaihtoehto. Jos rivi katoaisi tiedostosta, ainoa tapa löytää se olisi lukea
    lähdekoodia -- eikä käyttäjä koodaa itse.
    """
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    assert "# demos_root = " in text
    # Perustelu on rivin vieressä eikä muistissa.
    assert "Files On-Demand" in text
