"""Komentorivikuori (Typer).

CLI on ohut: se lukee asetukset, valitsee vaiheet ja näyttää tuloksen. Se ei
kutsu adaptereita eikä arkistoa suoraan, eikä siinä ole analyysilogiikkaa --
sama putki ajetaan myöhemmin web-kuoren takaa muuttamatta domainia.

Komentoja on kaksi: ``info`` näyttää asetukset, arkiston tilan ja avainten
tilan paljastamatta avainten arvoja, ja ``parse`` ajaa putken ensimmäisen
vaiheen yhdelle demolle. Loput (``scout``, ``next``, ``collect``, ``import``,
``report``) tulevat myöhemmissä storyissa.

Arkistoon ja adaptereihin ei kosketa täältä: polut pyydetään
``stages.archive_paths``ilta ja demoportti ``stages.parse.default_parser``ilta.
Riippuvuusnuoli on ``cli -> stages -> {domain, adapters, archive}``.

Käyttäjä ei koodaa itse, joten mikään virhe ei saa päätyä ruudulle raakana
pinojälkenä: :func:`main` muuntaa ne suomenkielisiksi viesteiksi ja
paluukoodeiksi.
"""

from __future__ import annotations

import sys

import typer

from pappascout import __version__
from pappascout.domain.models import Settings, load_settings, secrets_env_path
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult, archive_paths
from pappascout.stages import parse as parse_stage

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
    archive = archive_paths(settings.project)
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


@app.command("parse")
def parse(
    kohde: str = typer.Argument(
        ...,
        metavar="TIEDOSTO|MAP_DEMO_ID",
        help=(
            "Demotiedoston polku tai map_demo_id, jolloin demo etsitään "
            "arkiston demos- ja import-hakemistoista."
        ),
    ),
    pakota: bool = typer.Option(
        False,
        "--pakota",
        help=(
            "Parsi vaikka manifesti täsmäisi. Käytä, jos epäilet että arkiston "
            "tulos on vanhentunut."
        ),
    ),
) -> None:
    """Parsi demo kierrostauluksi.

    Kirjoittaa ``parsed/<map_demo_id>/rounds.parquet``-taulun ja sen
    manifestin. Jos manifesti täsmää, vaihe ohitetaan eikä demoa lueta
    uudelleen.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    map_demo_id, demo_path = parse_stage.resolve_demo(archive, kohde)

    # 233 MB:n demo vie sekunteja, pakattu enemmän. Ilman tätä riviä käyttäjä
    # katsoo tyhjää ruutua eikä tiedä, käynnistyikö mikään.
    typer.echo(f"Parsitaan {map_demo_id} ({demo_path.name})...", err=True)

    tulos = parse_stage.run(
        settings.parse,
        archive,
        map_demo_id,
        parse_stage.default_parser(),
        demo_path=demo_path,
        force=pakota,
    )
    typer.echo(_render_parse(tulos, regulation_rounds=2 * settings.league.mr))


#: Tulosteen sarakeleveys, jotta arvot linjautuvat otsikoiden alle.
_PARSE_LABEL_WIDTH = 20


def _rivi(otsikko: str, arvo: str) -> str:
    """Muotoile yksi tulosterivi tasalevyisellä otsikkosarakkeella."""
    return f"  {otsikko:<{_PARSE_LABEL_WIDTH}}{arvo}"


def _render_parse(tulos: StageResult, regulation_rounds: int) -> str:
    """Kokoa ``parse``-komennon tuloste.

    Erotettu omaksi funktiokseen, jotta tuloste on testattavissa ilman
    komentorivin ajamista.

    Args:
        tulos: Vaiheen palauttama tulos.
        regulation_rounds: Säännönmukaisten kierrosten määrä (MR12 -> 24).
            Tätä käytetään **vain** tulosteen jatkoaikamaininnassa; vaihe itse
            ei näe liiga- eikä kynnysasetuksia (AD-3).
    """
    stats = tulos.stats
    rivit: list[str] = []

    rivit.append(f"{'Ohitettu' if tulos.skipped else 'Parsittu'}: {tulos.unit}")

    # AD-9: tila näytetään aina kun se ei ole ok, jottei epäonnistunut yksikkö
    # näytä onnistuneelta.
    if tulos.status != "ok":
        rivit.append(_rivi("Tila", str(tulos.status)))
    if tulos.reason:
        rivit.append(_rivi("Syy", tulos.reason))

    if "unreadable" in stats:
        rivit.append(_rivi("Kierrokset", f"lukuja ei saatu ({stats['unreadable']})"))
        rivit.append(_rivi("Ajoaika", _sekunnit(tulos.duration_s)))
        return "\n".join(rivit)

    kierrokset = int(stats.get("rounds", 0) or 0)
    rivit.append(
        _rivi("Kierrokset", f"{kierrokset} (rivejä {int(stats.get('rows', 0) or 0)})")
    )

    suurin = int(stats.get("max_round_no", 0) or 0)
    if suurin > regulation_rounds:
        rivit.append(
            _rivi(
                "Jatkoaika",
                f"kyllä -- kierroksia {suurin}, säännönmukaisia {regulation_rounds}",
            )
        )
    else:
        rivit.append(_rivi("Jatkoaika", f"ei ({suurin}/{regulation_rounds})"))

    # Ohitetussa ajossa lukua ei ole: numeroimattomat kierrokset eivät ole
    # taulussa, joten niiden määrää ei voi lukea valmiista tuloksesta.
    ohitetut = int(stats.get("skipped_rounds", 0) or 0)
    if not tulos.skipped and ohitetut:
        rivit.append(
            _rivi(
                "Ohitetut kierrokset",
                f"{ohitetut} (warmup, puukkokierros ja uudelleenkäynnistykset)",
            )
        )

    ankkuriton = int(stats.get("no_freeze_end", 0) or 0)
    if ankkuriton:
        rivit.append(
            _rivi(
                "Ilman ankkuria",
                f"{ankkuriton} (freezetime-tick puuttuu, kierros silti mukana)",
            )
        )

    if "tick_rate" in stats and not stats.get("tick_rate_measured", True):
        rivit.append(
            _rivi(
                "Tickrate",
                f"{stats['tick_rate']:g} (oletus -- demosta ei saatu mitattua)",
            )
        )

    for polku in tulos.outputs:
        rivit.append(_rivi("Tulos", str(polku)))
    if tulos.manifest_path is not None:
        rivit.append(_rivi("Manifesti", str(tulos.manifest_path)))
    rivit.append(_rivi("Ajoaika", _sekunnit(tulos.duration_s)))

    return "\n".join(rivit)


def _sekunnit(arvo: float) -> str:
    """Sekunnit suomalaisella desimaalipilkulla."""
    return f"{arvo:.1f} s".replace(".", ",")


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
