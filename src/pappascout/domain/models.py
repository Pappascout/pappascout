"""Typatut asetusmallit (AD-3) ja niiden lataus.

Asetukset on jaettu viiteen osioon, joista jokainen on oma mallinsa. Osiointi on
**rakenteellinen**: vaihe ottaa parametrikseen vain oman osansa
(``parse(ParseSettings, ...)``, ``classify(ThresholdSettings, LeagueSettings, ...)``),
joten se ei pysty lukemaan muita osioita. Tämä on se mekanismi, joka tekee
lupauksesta "kynnysmuutos ei uudelleenparsi" rakenteellisen: ``parse``-manifestin
parametrihash lasketaan vain ``[parse]``-osiosta, eikä vaihe voi vahingossa
riippua ``[thresholds]``-arvoista, koska se ei näe niitä.

Avaimet eivät ole ``settings.toml``-tiedostossa. Ne luetaan
``%USERPROFILE%\\.pappascout\\.env``-tiedostosta ``SecretStr``-tyyppisinä;
projektin oma ``.env`` luetaan vain varalta. Syy on OneDrive: se tekisi
projektin ``.env``-tiedostosta konfliktikopioita kahdella koneella ja säilyttäisi
kierrätetyn avaimen versiohistoriassa.

Kaikki dollarimääräiset kynnysarvot ovat **per pelaaja**, ellei nimessä lue
muuta. Lähtöarvojen lähteet on merkitty ``settings.toml``-tiedostoon riveittäin.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic import ValidationError as _ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from pappascout.errors import SettingsError

__all__ = [
    "ProjectSettings",
    "LeagueSettings",
    "ParseSettings",
    "ThresholdSettings",
    "EconomySettings",
    "Settings",
    "load_settings",
    "secrets_env_path",
    "project_env_path",
    "settings_search_paths",
    "find_settings_file",
    "SETTINGS_FILENAME",
    "SETTINGS_ENV_VAR",
    "SETTINGS_SECTIONS",
]

SETTINGS_FILENAME = "settings.toml"
SETTINGS_ENV_VAR = "PAPPASCOUT_SETTINGS"

#: Ainoat sallitut ylätason avaimet ``settings.toml``-tiedostossa (AD-3).
SETTINGS_SECTIONS: frozenset[str] = frozenset(
    {"project", "league", "parse", "thresholds", "economy"}
)

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class _Section(BaseModel):
    """Asetusosion kantaluokka: tuntematon avain on virhe, ei hiljainen ohitus."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectSettings(_Section):
    """``[project]`` -- oma joukkue, arkisto ja ajon perusasetukset."""

    own_team_name: str
    archive_root: Path
    language: Literal["fi"] = "fi"
    lock_ttl_seconds: PositiveInt = 600


class LeagueSettings(_Section):
    """``[league]`` -- Pappaliigan kausi, karttapooli ja sääntökirjan arvot.

    Luetaan ``select``-, ``classify``-, ``aggregate``- ja ``render``-vaiheissa.
    """

    season: PositiveInt
    organizer_id: str
    championship_ids: list[str] = Field(min_length=1)
    map_pool: list[str] = Field(min_length=1)
    own_default_bans: list[str] = Field(default_factory=list)
    mr: PositiveInt = 12
    ot_start_money: PositiveInt = 12500

    @model_validator(mode="after")
    def _check_bans_are_in_pool(self) -> "LeagueSettings":
        ulkopuolella = [m for m in self.own_default_bans if m not in self.map_pool]
        if ulkopuolella:
            raise ValueError(
                "own_default_bans sisältää kartan, jota ei ole karttapoolissa: "
                f"{', '.join(ulkopuolella)}. Poistuiko kartta Active Dutysta?"
            )
        return self


class ParseSettings(_Section):
    """``[parse]`` -- ainoa osio, jonka ``parse``-vaihe näkee.

    ``parse``-manifestin parametrihash lasketaan vain tästä osiosta ja
    demoparser2:n versiosta, joten kynnysten säätö ei aiheuta uudelleenparsintaa.
    """

    snapshot_seconds: list[float] = Field(min_length=1)
    #: Aseet, jotka eivät kelpaa ensikontaktiksi (AD-5: "ase ei ole utility").
    first_contact_exclude_weapons: list[str] = Field(default_factory=list)
    #: Jos ensikontaktia ei löydy player_hurt-tapahtumista, käytetäänkö
    #: ensimmäistä player_death-tapahtumaa.
    first_contact_fallback_death: bool = True
    #: Enimmäisetäisyys pelin yksiköissä, jolta räjähdyksen alue saa napata
    #: lähimmän elossa olevan pelaajan alueen. None = ei napsautusta, jolloin
    #: detonate_area jää nulliksi. Kalibroidaan Epicissä 2 oikeilla demoilla.
    area_snap_units: PositiveInt | None = None


class ThresholdSettings(_Section):
    """``[thresholds]`` -- kaikki luokittelun ja otannan rajat.

    Nämä ovat kalibroitavia lähtöarvoja, eivät lopullisia totuuksia. Story 1.4
    säätää ne oikeilla Pappaliiga-demoilla. Rahamäärät ovat dollareita
    **per pelaaja**.
    """

    # Kierrosnumeroon perustuvat säännöt (AD-4 vaiheet 1 ja 2)
    pistol_rounds: list[PositiveInt] = Field(min_length=1)
    regulation_rounds: PositiveInt = 24

    # Varustearvorajat (AD-4 vaiheet 3 ja 5)
    full_equip_min: PositiveInt = 4000
    half_equip_min: PositiveInt = 3000
    anomaly_equip_max_after_win: PositiveInt = 2000

    # Raharajat (AD-4 vaihe 4)
    eco_money_max: PositiveInt = 2000
    eco_loss_count_min: NonNegativeInt = 2
    eco_money_max_low_loss: PositiveInt = 3000
    force_money_min: PositiveInt = 1500
    force_money_max: PositiveInt = 2500

    # Loss count -säännöt
    loss_count_half_start: NonNegativeInt = 1
    loss_count_min: NonNegativeInt = 0
    loss_count_max: PositiveInt = 4

    # Joukkueidentiteetti ja rosterikynnys (AD-6)
    team_identity_min_common: PositiveInt = 3
    roster_size: PositiveInt = 5
    roster_min_regulars: PositiveInt = 4

    # Otanta ja poikkeamat (AD-10)
    small_sample_rounds: PositiveInt = 3
    stack_min_players: PositiveInt = 4

    @model_validator(mode="after")
    def _check_ranges_are_consistent(self) -> "ThresholdSettings":
        if self.half_equip_min >= self.full_equip_min:
            raise ValueError(
                f"half_equip_min ({self.half_equip_min}) on oltava pienempi kuin "
                f"full_equip_min ({self.full_equip_min}); muuten puoliostoa ei "
                "voi koskaan erottaa täydestä ostosta."
            )
        if self.force_money_min > self.force_money_max:
            raise ValueError(
                f"force_money_min ({self.force_money_min}) on suurempi kuin "
                f"force_money_max ({self.force_money_max}); väli on tyhjä."
            )
        if self.loss_count_min >= self.loss_count_max:
            raise ValueError(
                f"loss_count_min ({self.loss_count_min}) on oltava pienempi kuin "
                f"loss_count_max ({self.loss_count_max})."
            )
        if not (
            self.loss_count_min
            <= self.loss_count_half_start
            <= self.loss_count_max
        ):
            raise ValueError(
                f"loss_count_half_start ({self.loss_count_half_start}) on "
                f"rajojen {self.loss_count_min}-{self.loss_count_max} ulkopuolella."
            )
        if self.roster_min_regulars > self.roster_size:
            raise ValueError(
                f"roster_min_regulars ({self.roster_min_regulars}) ei voi olla "
                f"suurempi kuin roster_size ({self.roster_size})."
            )
        return self


class EconomySettings(_Section):
    """``[economy]`` -- CS2:n talousmalli raporttiliitteen laskelmia varten.

    Näitä ei käytetä kierroksen luokitteluun; luokittelu nojaa havaittuun rahaan
    ja varustearvoon. Talousmalli selittää raportissa, miksi joukkueella oli se
    raha joka sillä oli.
    """

    start_money: PositiveInt = 800
    max_money: PositiveInt = 16000
    loss_bonus_steps: list[PositiveInt] = Field(min_length=1)
    win_reward_elimination: PositiveInt = 3250
    win_reward_bomb: PositiveInt = 3500
    plant_bonus_loss: PositiveInt = 600
    plant_reward: PositiveInt = 300
    defuse_reward: PositiveInt = 300
    ct_kill_bonus: NonNegativeInt = 50
    short_handed_bonus: NonNegativeInt = 1000
    #: Tapporaha aseluokittain; poikkeukset ase kerrallaan.
    kill_rewards: dict[str, int] = Field(default_factory=dict)
    #: Ostovalikon hinnat. Käytetään puoliostojen erotteluun raportissa.
    prices: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_loss_bonus_is_ascending(self) -> "EconomySettings":
        askeleet = self.loss_bonus_steps
        if any(a >= b for a, b in zip(askeleet, askeleet[1:])):
            raise ValueError(
                f"loss_bonus_steps ei ole nouseva: {askeleet}. "
                "Portaiden on kasvettava, koska laskuri kasvaa häviöistä."
            )
        if self.start_money > self.max_money:
            raise ValueError(
                f"start_money ({self.start_money}) on suurempi kuin max_money "
                f"({self.max_money})."
            )
        return self


class Settings(BaseSettings):
    """Koko asetuskokonaisuus: viisi osiota ja koneen omat avaimet.

    Vaiheelle ei anneta tätä oliota vaan yksi osio kerrallaan (AD-3).
    """

    # extra="ignore": koneen .env saa sisältää muutakin kuin nämä avaimet ilman
    # että lataus kaatuu. Asetustiedoston tuntemattomat osiot tarkistetaan
    # erikseen load_settings-funktiossa, jotta kirjoitusvirhe ei mene hiljaa läpi.
    model_config = SettingsConfigDict(
        extra="ignore",
        frozen=True,
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    project: ProjectSettings
    league: LeagueSettings
    parse: ParseSettings
    thresholds: ThresholdSettings
    economy: EconomySettings

    faceit_api_key: SecretStr | None = None
    faceit_downloads_token: SecretStr | None = None

    #: Mistä avaimet luettiin -- vain virheilmoituksia ja ``info``-komentoa varten.
    secrets_file: Path | None = None
    #: Mistä asetustiedosto luettiin.
    settings_file: Path | None = None

    @model_validator(mode="after")
    def _check_sections_agree(self) -> "Settings":
        """Osioiden väliset ristiriidat kiinni jo latausvaiheessa.

        Nämä ovat eri osioissa mutta kuvaavat samaa asiaa: ottelun formaattia.
        Jos ne eroavat, luokittelu tuottaisi hiljaa vääriä kierrostyyppejä koko
        arkiston läpi.
        """
        odotettu_saannonmukaiset = 2 * self.league.mr
        if self.thresholds.regulation_rounds != odotettu_saannonmukaiset:
            raise ValueError(
                f"thresholds.regulation_rounds ({self.thresholds.regulation_rounds}) "
                f"ei vastaa liigan formaattia MR{self.league.mr}, joka tarkoittaa "
                f"{odotettu_saannonmukaiset} säännönmukaista kierrosta."
            )
        odotetut_pistoolit = [1, self.league.mr + 1]
        if self.thresholds.pistol_rounds != odotetut_pistoolit:
            raise ValueError(
                f"thresholds.pistol_rounds ({self.thresholds.pistol_rounds}) ei "
                f"vastaa liigan formaattia MR{self.league.mr}: pistoolikierrokset "
                f"ovat {odotetut_pistoolit} (kierros 1 ja puoliajan 1. kierros)."
            )
        odotetut_portaat = self.thresholds.loss_count_max + 1
        if len(self.economy.loss_bonus_steps) != odotetut_portaat:
            raise ValueError(
                f"economy.loss_bonus_steps sisältää "
                f"{len(self.economy.loss_bonus_steps)} porrasta, mutta loss count "
                f"vaihtelee välillä {self.thresholds.loss_count_min}-"
                f"{self.thresholds.loss_count_max} eli portaita tarvitaan "
                f"{odotetut_portaat}. Laskuri indeksoi tätä listaa suoraan."
            )
        return self

    def require_faceit_api_key(self) -> str:
        """Palauta FACEIT Data API -avain tai kerro suomeksi, miten se asetetaan."""
        return self._require_secret(self.faceit_api_key, "FACEIT_API_KEY")

    def require_faceit_downloads_token(self) -> str:
        """Palauta FACEIT Downloads -token tai kerro suomeksi, miten se asetetaan."""
        return self._require_secret(
            self.faceit_downloads_token, "FACEIT_DOWNLOADS_TOKEN"
        )

    def _require_secret(self, value: SecretStr | None, name: str) -> str:
        if value is not None and value.get_secret_value().strip():
            return value.get_secret_value()
        polku = self.secrets_file or secrets_env_path()
        raise SettingsError(
            f"Avainta {name} ei löytynyt.\n"
            f"Lisää tiedostoon {polku} rivi:\n"
            f"    {name}=<oma avaimesi>\n"
            "Tiedosto on koneen oma eikä se ole OneDrivessa tai versionhallinnassa."
        )

    def secret_status(self, name: str) -> str:
        """Palauta avaimen tila sanana -- ``asetettu`` tai ``puuttuu``.

        Ei koskaan palauta itse avainta.
        """
        value = getattr(self, name.lower(), None)
        if isinstance(value, SecretStr) and value.get_secret_value().strip():
            return "asetettu"
        return "puuttuu"


def secrets_env_path() -> Path:
    """Koneen oma avaintiedosto ``%USERPROFILE%\\.pappascout\\.env``.

    Tarkoituksella OneDriven ulkopuolella: OneDrive tekisi tiedostosta
    konfliktikopioita kahdella koneella ja säilyttäisi kierrätetyn avaimen
    versiohistoriassa.
    """
    return Path.home() / ".pappascout" / ".env"


def project_env_path(start: Path | None = None) -> Path:
    """Projektin oma ``.env``, jota käytetään vain varalta."""
    return (start or Path.cwd()) / ".env"


def _repo_root() -> Path:
    """Repon juuri paketin sijainnista laskettuna (src-layout)."""
    return Path(__file__).resolve().parents[3]


def settings_search_paths(start: Path | None = None) -> list[Path]:
    """Polut, joista ``settings.toml`` etsitään, tärkeysjärjestyksessä."""
    paths: list[Path] = []
    from_env = os.environ.get(SETTINGS_ENV_VAR)
    if from_env:
        paths.append(Path(from_env))

    cwd = (start or Path.cwd()).resolve()
    for directory in [cwd, *cwd.parents]:
        paths.append(directory / SETTINGS_FILENAME)

    paths.append(_repo_root() / SETTINGS_FILENAME)

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def find_settings_file(start: Path | None = None) -> Path:
    """Etsi ``settings.toml`` tai kerro suomeksi, mistä sitä etsittiin.

    Jos ympäristömuuttuja on asetettu, se on käsky eikä ehdotus: puuttuva
    tiedosto on virhe, ei syy pudota takaisin työhakemistoon. Hiljainen
    varasija lukisi eri asetukset kuin käyttäjä pyysi.
    """
    from_env = os.environ.get(SETTINGS_ENV_VAR)
    if from_env:
        pyydetty = Path(from_env)
        if not pyydetty.is_file():
            raise SettingsError(
                f"Ympäristömuuttuja {SETTINGS_ENV_VAR} osoittaa tiedostoon "
                f"{pyydetty}, jota ei ole.\n"
                "Korjaa polku tai poista muuttuja, jolloin settings.toml "
                "etsitään työhakemistosta."
            )
        return pyydetty

    candidates = settings_search_paths(start)
    for path in candidates:
        if path.is_file():
            return path
    listaus = "\n".join(f"    {path}" for path in candidates)
    raise SettingsError(
        "Asetustiedostoa settings.toml ei löytynyt.\n"
        "Etsin näistä poluista:\n"
        f"{listaus}\n"
        "Siirry projektin juureen tai aseta ympäristömuuttuja "
        f"{SETTINGS_ENV_VAR} osoittamaan tiedostoon."
    )


def load_settings(
    settings_file: Path | None = None,
    env_files: tuple[Path, ...] | None = None,
) -> Settings:
    """Lataa asetukset TOML-tiedostosta ja avaimet ``.env``-tiedostoista.

    Args:
        settings_file: Asetustiedoston polku. Oletuksena etsitään
            :func:`settings_search_paths` -järjestyksessä.
        env_files: Avaintiedostot heikoimmasta vahvimpaan. Oletuksena projektin
            ``.env`` ensin ja koneen oma ``.env`` viimeisenä, jolloin koneen oma
            voittaa.

    Returns:
        Validoitu :class:`Settings`.

    Raises:
        SettingsError: Jos tiedostoa ei löydy, se ei ole kelvollista TOMLia tai
            jokin arvo ei kelpaa. Viesti kertoo aina, mitä pitää korjata.
    """
    path = Path(settings_file) if settings_file is not None else find_settings_file()
    if not path.is_file():
        raise SettingsError(
            f"Asetustiedostoa ei löytynyt polusta {path}.\n"
            "Luo tiedosto tai anna oikea polku."
        )

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(
            f"Asetustiedosto {path} ei ole kelvollista TOMLia: {exc}\n"
            "Korjaa syntaksi ja aja komento uudelleen."
        ) from exc
    except OSError as exc:
        raise SettingsError(
            f"Asetustiedostoa {path} ei voitu lukea: {exc}"
        ) from exc

    unknown = sorted(set(data) - SETTINGS_SECTIONS)
    if unknown:
        raise SettingsError(
            f"Asetustiedostossa {path} on tuntematon osio tai avain: "
            f"{', '.join(unknown)}.\n"
            f"Sallitut osiot ovat {', '.join(sorted(SETTINGS_SECTIONS))}.\n"
            "Avaimia ei kirjoiteta tähän tiedostoon vaan tiedostoon "
            f"{secrets_env_path()}."
        )

    if env_files is None:
        env_files = (project_env_path(path.parent), secrets_env_path())
    # pydantic-settings: listan viimeinen tiedosto voittaa.
    existing = [str(p) for p in env_files if Path(p).is_file()]
    secrets_file = Path(existing[-1]) if existing else None

    try:
        return Settings(
            _env_file=existing or None,
            settings_file=path,
            secrets_file=secrets_file,
            **data,
        )
    except _ValidationError as exc:
        raise SettingsError(
            f"Asetustiedosto {path} ei kelpaa:\n"
            f"{_format_validation_error(exc)}\n"
            "Korjaa arvot ja aja komento uudelleen."
        ) from exc


def _format_validation_error(exc: _ValidationError) -> str:
    """Muotoile pydanticin virheet lyhyeksi suomenkieliseksi listaksi."""
    rivit = []
    for error in exc.errors():
        kohta = ".".join(str(part) for part in error["loc"]) or "(juuri)"
        rivit.append(f"    {kohta}: {error['msg']}")
    return "\n".join(rivit)
