"""Komentorivikuori (Typer).

CLI on ohut: se lukee asetukset, valitsee vaiheet ja näyttää tuloksen. Se ei
kutsu adaptereita eikä arkistoa suoraan, eikä siinä ole analyysilogiikkaa --
sama putki ajetaan myöhemmin web-kuoren takaa muuttamatta domainia.

Tässä storyssa on vain ``info``, joka todistaa että runko toimii: se näyttää
asetukset, arkiston tilan ja avainten tilan paljastamatta avainten arvoja.
Putken komennot (``parse``, ``scout``, ``next``, ``collect``, ``import``,
``report``) tulevat myöhemmissä storyissa.

Käyttäjä ei koodaa itse, joten mikään virhe ei saa päätyä ruudulle raakana
pinojälkenä: :func:`main` muuntaa ne suomenkielisiksi viesteiksi ja
paluukoodeiksi.
"""

from __future__ import annotations

import sys

import typer

from pappascout import __version__
from pappascout.archive.paths import ArchivePaths
from pappascout.domain.models import Settings, load_settings, secrets_env_path
from pappascout.errors import PappascoutError

__all__ = ["app", "main"]

app = typer.Typer(
    name="pappascout",
    help="Pappaliigan CS2-vastustajascouting: demoista kierrostyypit ja raportti.",
    no_args_is_help=True,
    add_completion=False,
)

_SECRET_NAMES = ("FACEIT_API_KEY", "FACEIT_DOWNLOADS_TOKEN")

#: Paluukoodit. 0 = onnistui, 1 = odotettu virhe, 2 = odottamaton virhe.
EXIT_KNOWN_ERROR = 1
EXIT_UNEXPECTED_ERROR = 2

_SIZE_UNITS = ("kt", "Mt", "Gt", "Tt", "Pt")


def _human_size(num_bytes: int) -> str:
    """Muotoile tavumäärä luettavaksi.

    Desimaalierottimena on pilkku suomalaisen käytännön mukaisesti. Alle
    kilotavun määrät näytetään tarkkoina tavuina.

    >>> _human_size(1536)
    '1,5 kt'
    """
    if num_bytes < 1024:
        return f"{num_bytes} tavua"
    arvo = float(num_bytes)
    yksikko = _SIZE_UNITS[0]
    for yksikko in _SIZE_UNITS:
        arvo /= 1024
        if arvo < 1024:
            break
    return f"{arvo:.1f} {yksikko}".replace(".", ",")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pappascout {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(  # noqa: ARG001 - Typerin callback-konventio
        False,
        "--version",
        help="Näytä versio ja lopeta.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Pappascout."""


@app.command("info")
def info(
    koko: bool = typer.Option(
        False,
        "--koko",
        help=(
            "Laske myös arkiston yhteiskoko. Oletuksena pois päältä, koska se "
            "lukee koko hakemistopuun läpi."
        ),
    ),
) -> None:
    """Näytä asetukset, arkiston tila ja avainten tila.

    Avainten arvoja ei tulosteta koskaan -- vain tieto siitä, onko avain
    asetettu.
    """
    settings = load_settings()
    typer.echo(_render_info(settings, show_size=koko))


def _render_info(settings: Settings, show_size: bool = False) -> str:
    """Kokoa ``info``-komennon tuloste.

    Erotettu omaksi funktiokseen, jotta tuloste on testattavissa ilman
    komentorivin ajamista.

    Args:
        settings: Ladatut asetukset.
        show_size: Lasketaanko arkiston yhteiskoko. Oletuksena ei: arkistossa on
            satoja megatavuja demoja, ja koko puun läpikäynti tekisi nopeasta
            tilannekatsauksesta hitaan.
    """
    archive = ArchivePaths.from_settings(settings.project.archive_root)
    rivit: list[str] = []

    rivit.append(f"Pappascout {__version__}")
    rivit.append("")

    rivit.append("Asetukset")
    rivit.append(f"  Asetustiedosto     {settings.settings_file}")
    rivit.append(f"  Oma joukkue        {settings.project.own_team_name}")
    rivit.append(f"  Kieli              {settings.project.language}")
    rivit.append(f"  Kausi              {settings.league.season}")
    rivit.append(f"  Championshipit     {', '.join(settings.league.championship_ids)}")
    rivit.append(f"  Karttapooli        {', '.join(settings.league.map_pool)}")
    rivit.append(
        "  Omat vakiobanit    "
        + (", ".join(settings.league.own_default_bans) or "ei asetettu")
    )
    rivit.append(
        f"  Formaatti          MR{settings.league.mr}, "
        f"jatkoajan aloitusraha {settings.league.ot_start_money} $"
    )
    rivit.append(
        "  Näytepisteet       "
        + ", ".join(f"{s:g} s" for s in settings.parse.snapshot_seconds)
    )
    rivit.append(
        f"  Täyden oston raja  {settings.thresholds.full_equip_min} $ / pelaaja"
    )
    rivit.append(
        "  Pistoolikierrokset "
        + ", ".join(str(r) for r in settings.thresholds.pistol_rounds)
        + f"; säännönmukaisia kierroksia {settings.thresholds.regulation_rounds}"
    )
    rivit.append("")

    rivit.append("Arkisto")
    rivit.append(f"  Polku              {archive.root}")
    if not archive.exists():
        rivit.append(
            "  Tila               puuttuu -- hakemisto luodaan ensimmäisellä ajolla"
        )
    elif show_size:
        rivit.append(f"  Tila               löytyy, {_human_size(archive.total_size_bytes())}")
    else:
        rivit.append("  Tila               löytyy")
        rivit.append("  Koko               ei laskettu (--koko laskee sen)")
    rivit.append("")

    rivit.append("Avaimet")
    rivit.append(f"  Tiedosto           {settings.secrets_file or secrets_env_path()}")
    leveys = max(len(nimi) for nimi in _SECRET_NAMES)
    for nimi in _SECRET_NAMES:
        rivit.append(f"  {nimi:<{leveys}} {settings.secret_status(nimi)}")

    return "\n".join(rivit)


def main() -> None:
    """Ohjelman sisäänkäynti.

    Muuntaa poikkeukset käyttäjälle ymmärrettäviksi:

    * :class:`~pappascout.errors.PappascoutError` -- odotettu tilanne, jonka
      viesti kertoo mitä tehdä seuraavaksi. Paluukoodi 1.
    * mikä tahansa muu poikkeus -- ohjelmavirhe, josta näytetään lyhyt
      suomenkielinen rivi eikä pinojälkeä. Paluukoodi 2.

    Käyttäjä ei koodaa itse, joten pinojäljestä ei ole hänelle hyötyä.
    """
    try:
        app()
    except PappascoutError as exc:
        typer.secho(f"Virhe: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(EXIT_KNOWN_ERROR)
    except Exception as exc:  # noqa: BLE001 - viimeinen suoja käyttäjän edessä
        typer.secho(
            f"Odottamaton virhe: {exc}\n"
            "Tämä on ohjelmavirhe. Kokeile uudelleen tai kirjaa tapaus ylös.",
            fg=typer.colors.RED,
            err=True,
        )
        sys.exit(EXIT_UNEXPECTED_ERROR)


if __name__ == "__main__":  # pragma: no cover
    main()
