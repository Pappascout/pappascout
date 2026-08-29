"""Komentorivikuori (Typer).

CLI on ohut: se lukee asetukset, valitsee vaiheet ja näyttää tuloksen. Se ei
kutsu adaptereita eikä arkistoa suoraan, eikä siinä ole analyysilogiikkaa --
sama putki ajetaan myöhemmin web-kuoren takaa muuttamatta domainia.

Komentoja on kolme: ``info`` näyttää asetukset, arkiston tilan ja avainten
tilan paljastamatta avainten arvoja, ``parse`` ajaa putken ensimmäisen vaiheen
yhdelle demolle ja ``classify`` luokittelee sen kierrokset yhden joukkueen
näkökulmasta. Loput (``scout``, ``next``, ``collect``, ``import``, ``report``)
tulevat myöhemmissä storyissa.

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
from pappascout.constants import ROUND_TYPES, UNCLASSIFIED
from pappascout.domain.models import Settings, load_settings, secrets_env_path
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult, archive_paths
from pappascout.stages import classify as classify_stage
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
    value = float(num_bytes)
    unit = _SIZE_UNITS[0]
    for unit in _SIZE_UNITS:
        value /= 1024
        if value < 1024:
            break
    return f"{value:.1f} {unit}".replace(".", ",")


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
    size: bool = typer.Option(
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
    typer.echo(_render_info(settings, show_size=size))


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
    lines: list[str] = []

    lines.append(f"Pappascout {__version__}")
    lines.append("")

    lines.append("Asetukset")
    lines.append(f"  Asetustiedosto     {settings.settings_file}")
    lines.append(f"  Oma joukkue        {settings.project.own_team_name}")
    lines.append(f"  Kieli              {settings.project.language}")
    lines.append(f"  Kausi              {settings.league.season}")
    lines.append(f"  Championshipit     {', '.join(settings.league.championship_ids)}")
    lines.append(f"  Karttapooli        {', '.join(settings.league.map_pool)}")
    lines.append(
        "  Omat vakiobanit    "
        + (", ".join(settings.league.own_default_bans) or "ei asetettu")
    )
    lines.append(
        f"  Formaatti          MR{settings.league.mr}, "
        f"jatkoajan aloitusraha {settings.league.ot_start_money} $"
    )
    lines.append(
        "  Näytepisteet       "
        + ", ".join(f"{s:g} s" for s in settings.parse.snapshot_seconds)
    )
    lines.append(
        f"  Täyden oston raja  {settings.thresholds.full_equip_min} $ / pelaaja"
    )
    lines.append(
        "  Pistoolikierrokset "
        + ", ".join(str(r) for r in settings.thresholds.pistol_rounds)
        + f"; säännönmukaisia kierroksia {settings.thresholds.regulation_rounds}"
    )
    lines.append("")

    lines.append("Arkisto")
    lines.append(f"  Polku              {archive.root}")
    if not archive.exists():
        lines.append(
            "  Tila               puuttuu -- hakemisto luodaan ensimmäisellä ajolla"
        )
    elif show_size:
        lines.append(f"  Tila               löytyy, {_human_size(archive.total_size_bytes())}")
    else:
        lines.append("  Tila               löytyy")
        lines.append("  Koko               ei laskettu (--koko laskee sen)")
    lines.append("")

    lines.append("Avaimet")
    lines.append(f"  Tiedosto           {settings.secrets_file or secrets_env_path()}")
    width = max(len(name) for name in _SECRET_NAMES)
    for name in _SECRET_NAMES:
        lines.append(f"  {name:<{width}} {settings.secret_status(name)}")

    return "\n".join(lines)


@app.command("parse")
def parse(
    target: str = typer.Argument(
        ...,
        metavar="TIEDOSTO|MAP_DEMO_ID",
        help=(
            "Demotiedoston polku tai map_demo_id, jolloin demo etsitään "
            "arkiston demos- ja import-hakemistoista."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--pakota",
        help=(
            "Parsi vaikka manifesti täsmäisi. Käytä, jos epäilet että arkiston "
            "tulos on vanhentunut."
        ),
    ),
) -> None:
    """Parsi demo kierros-, näytepiste- ja tapahtumatauluiksi.

    Kirjoittaa ``parsed/<map_demo_id>/rounds.parquet``-, ``ticks.parquet``- ja
    ``events.parquet``-taulut sekä niiden manifestin. Jos manifesti täsmää,
    vaihe ohitetaan eikä demoa lueta uudelleen.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    map_demo_id, demo_path = parse_stage.resolve_demo(archive, target)

    # 233 MB:n demo vie sekunteja, pakattu enemmän. Ilman tätä riviä käyttäjä
    # katsoo tyhjää ruutua eikä tiedä, käynnistyikö mikään.
    typer.echo(f"Parsitaan {map_demo_id} ({demo_path.name})...", err=True)

    result = parse_stage.run(
        settings.parse,
        archive,
        map_demo_id,
        parse_stage.default_parser(settings.parse),
        demo_path=demo_path,
        force=force,
    )
    typer.echo(_render_parse(result, regulation_rounds=2 * settings.league.mr))


#: Tulosteen sarakeleveys, jotta arvot linjautuvat otsikoiden alle.
_PARSE_LABEL_WIDTH = 20


def _line(label: str, value: str) -> str:
    """Muotoile yksi tulosterivi tasalevyisellä otsikkosarakkeella."""
    return f"  {label:<{_PARSE_LABEL_WIDTH}}{value}"


def _render_parse(result: StageResult, regulation_rounds: int) -> str:
    """Kokoa ``parse``-komennon tuloste.

    Erotettu omaksi funktiokseen, jotta tuloste on testattavissa ilman
    komentorivin ajamista.

    Args:
        result: Vaiheen palauttama tulos.
        regulation_rounds: Säännönmukaisten kierrosten määrä (MR12 -> 24).
            Tätä käytetään **vain** tulosteen jatkoaikamaininnassa; vaihe itse
            ei näe liiga- eikä kynnysasetuksia (AD-3).
    """
    stats = result.stats
    lines: list[str] = []

    lines.append(f"{'Ohitettu' if result.skipped else 'Parsittu'}: {result.unit}")

    # AD-9: tila näytetään aina kun se ei ole ok, jottei epäonnistunut yksikkö
    # näytä onnistuneelta.
    if result.status != "ok":
        lines.append(_line("Tila", str(result.status)))
    if result.reason:
        lines.append(_line("Syy", result.reason))

    if "unreadable" in stats:
        lines.append(_line("Kierrokset", f"lukuja ei saatu ({stats['unreadable']})"))
        lines.append(_line("Ajoaika", _seconds(result.duration_s)))
        return "\n".join(lines)

    rounds = int(stats.get("rounds", 0) or 0)
    lines.append(
        _line("Kierrokset", f"{rounds} (rivejä {int(stats.get('rows', 0) or 0)})")
    )

    max_round = int(stats.get("max_round_no", 0) or 0)
    if max_round > regulation_rounds:
        lines.append(
            _line(
                "Jatkoaika",
                f"kyllä -- kierroksia {max_round}, säännönmukaisia {regulation_rounds}",
            )
        )
    else:
        lines.append(_line("Jatkoaika", f"ei ({max_round}/{regulation_rounds})"))

    # Ohitetussa ajossa lukua ei ole: numeroimattomat kierrokset eivät ole
    # taulussa, joten niiden määrää ei voi lukea valmiista tuloksesta.
    skipped_rounds = int(stats.get("skipped_rounds", 0) or 0)
    if not result.skipped and skipped_rounds:
        lines.append(
            _line(
                "Ohitetut kierrokset",
                f"{skipped_rounds} (warmup, puukkokierros ja uudelleenkäynnistykset)",
            )
        )

    no_anchor = int(stats.get("no_freeze_end", 0) or 0)
    if no_anchor:
        lines.append(
            _line(
                "Ilman ankkuria",
                f"{no_anchor} (freezetime-tick puuttuu, kierros silti mukana)",
            )
        )

    lines.extend(_sample_points(stats, rounds))
    lines.extend(_utility(stats, rounds))

    if "tick_rate" in stats and not stats.get("tick_rate_measured", True):
        lines.append(
            _line(
                "Tickrate",
                f"{stats['tick_rate']:g} (oletus -- demosta ei saatu mitattua)",
            )
        )

    for path in result.outputs:
        lines.append(_line("Tulos", str(path)))
    if result.manifest_path is not None:
        lines.append(_line("Manifesti", str(result.manifest_path)))
    lines.append(_line("Ajoaika", _seconds(result.duration_s)))

    return "\n".join(lines)


def _sample_points(stats: dict, rounds: int) -> list[str]:
    """Näytepisteiden ja ensikontaktien rivit ``parse``-tulosteeseen.

    Ilman näitä käyttäjä ei näe, syntyikö asetelmadata lainkaan: kierrosluku
    näyttäisi samalta myös silloin, kun ``ticks.parquet`` on tyhjä. Nolla on
    siksi yhtä tärkeä kertoa kuin suuri luku, ja se sanotaan ääneen.
    """
    if "ticks_unreadable" in stats:
        return [
            _line(
                "Näytepisteet",
                f"lukuja ei saatu ({stats['ticks_unreadable']})",
            )
        ]
    if "tick_rows" not in stats:
        return []

    lines: list[str] = []
    points = int(stats.get("sample_points", 0) or 0)
    tick_rows = int(stats.get("tick_rows", 0) or 0)
    sampled_rounds = int(stats.get("sample_rounds", 0) or 0)

    if points:
        lines.append(
            _line(
                "Näytepisteet",
                f"{points} ({sampled_rounds}/{rounds} kierroksella, "
                f"rivejä {tick_rows})",
            )
        )
    else:
        lines.append(
            _line(
                "Näytepisteet",
                "0 -- asetelmadataa ei syntynyt",
            )
        )

    # Kierros ilman yhtään näytepistettä voi johtua kolmesta syystä: ankkuri
    # puuttuu, kierros ratkesi ennen ensimmäistä näytepistettä, tai
    # näytepisteajat ovat väärin. Erotus kerrotaan, syytä ei arvata.
    without_samples = rounds - sampled_rounds
    if without_samples > 0:
        lines.append(
            _line(
                "Ilman näytepistettä",
                f"{without_samples} kierrosta (ankkuri puuttuu tai kierros ratkesi "
                "ennen ensimmäistä näytepistettä)",
            )
        )

    contacts = int(stats.get("first_contact_rounds", 0) or 0)
    if contacts:
        lines.append(_line("Ensikontaktit", f"{contacts}/{rounds} kierroksella"))
    else:
        lines.append(
            _line(
                "Ensikontaktit",
                "0 -- yhdeltäkään kierrokselta ei löytynyt ristiinpuolista osumaa",
            )
        )

    # Adapterin omat havainnot: näitä ei voi laskea valmiista taulusta.
    partial = int(stats.get("partial_samples", 0) or 0)
    if partial:
        lines.append(
            _line(
                "Vajaat näytepisteet",
                f"{partial} (pelaajia vähemmän kuin täydellä pisteellä)",
            )
        )
    unknown = int(stats.get("unknown_side_events", 0) or 0)
    if unknown:
        lines.append(
            _line(
                "Puoli tuntematon",
                f"{unknown} vahinkotapahtumaa ohitettiin ensikontaktia "
                "etsittäessä",
            )
        )
    return lines


def _utility(stats: dict, rounds: int) -> list[str]:
    """Utility-tapahtumien rivit ``parse``-tulosteeseen.

    Neljä kysymystä, neljä lukua: **syntyikö** utilitydataa (heitot),
    **päättyikö** rata (räjähdykset), **osuiko** aluepäättely ja **katosiko**
    matkalla mitään. Nolla heittoa on kelvollinen tulos -- demossa on voitu
    jättää utility heittämättä -- mutta se sanotaan ääneen, koska kierrosluku
    näyttäisi muuten samalta myös rikkoutuneella lukemisella.
    """
    if "events_unreadable" in stats:
        return [_line("Utility", f"lukuja ei saatu ({stats['events_unreadable']})")]
    if "event_rows" not in stats:
        return []

    lines: list[str] = []
    throws = int(stats.get("utility_throws", 0) or 0)
    detonations = int(stats.get("utility_detonations", 0) or 0)
    utility_rounds = int(stats.get("utility_rounds", 0) or 0)

    if throws:
        lines.append(
            _line(
                "Utility",
                f"{throws} heittoa, {detonations} räjähdystä "
                f"({utility_rounds}/{rounds} kierroksella)",
            )
        )
    else:
        lines.append(_line("Utility", "0 heittoa -- utilitydataa ei syntynyt"))

    # Räjähtämätön kranaatti on normaali (pelaaja kuolee heitto kädessä), mutta
    # suuri erotus tarkoittaisi, ettei radan loppua tunnisteta. Toiseen suuntaan
    # se on mahdoton: räjähdys syntyy vain heiton parina, joten negatiivinen
    # erotus on vika eikä havainto -- eikä sitä tulosteta miinusmerkkisenä
    # "puuttuvien" lukuna.
    if detonations > throws:
        lines.append(
            _line(
                "Räjähdyksiä liikaa",
                f"{detonations - throws} enemmän kuin heittoja -- "
                "utility-taulu on epäjohdonmukainen",
            )
        )
    elif throws > detonations:
        lines.append(_line("Ilman räjähdystä", f"{throws - detonations} kranaattia"))

    if throws:
        lines.extend(_utility_areas(stats))

    # Adapterin ja vaiheen omat havainnot: pudotettua kranaattia ei näe
    # valmiista taulusta. Otsikot mahtuvat _PARSE_LABEL_WIDTHiin, jotta
    # arvosarake pysyy suorassa.
    for key, label, description in (
        (
            "grenades_without_thrower",
            "Ilman heittäjää",
            "lentorataa ohitettiin",
        ),
        (
            "grenades_outside_rounds",
            "Ilman kierrosta",
            "kranaattia (lämmittely tai kierroksen ratkeamisen jälkeen)",
        ),
        (
            "utility_unnumbered_rounds",
            "Ei kierrosnumeroa",
            "heittoa numeroimattomilta kierroksilta (warmup, puukkokierros)",
        ),
        (
            "grenades_unknown_side",
            "Ilman puolta",
            "kranaattia ohitettiin (heittäjän joukkue ei ratkennut)",
        ),
        (
            "grenades_unknown_type",
            "Tuntematon tyyppi",
            "kranaattia -- demoparser2:n luokkanimi ei ole listalla",
        ),
        (
            "grenades_fire_type_unresolved",
            "Tulityyppi auki",
            "kranaattia jäi molotoviksi (incendiary-erottelu ei ratkennut)",
        ),
        (
            "grenades_detonating_after_round",
            "Räjähdys myöhässä",
            "kierroksen päättymisen jälkeen -- alue jätettiin tyhjäksi",
        ),
        (
            "grenade_ticks_without_players",
            "Tickillä ei rivejä",
            "päätepistettä ilman pelaajia -- aluetta ei voitu edes yrittää",
        ),
        (
            "grenades_id_reused_in_round",
            "Tunniste toistuu",
            "kranaattiparia samalla tunnisteella kierroksen sisällä",
        ),
    ):
        count = int(stats.get(key, 0) or 0)
        if count:
            lines.append(_line(label, f"{count} {description}"))
    return lines


def _utility_areas(stats: dict) -> list[str]:
    """Alueen lähteet erikseen: havainto, arvio ja puuttuva.

    Kolme lukua eikä yksi, koska ne ovat eri laatua olevaa tietoa. Heiton alue
    on heittäjän oma ``m_szLastPlaceName`` eli havainto; räjähdyksen alue on
    lähimmältä pelaajalta johdettu arvio. Yhteen niputettuna raportin lukija
    luulisi molempia yhtä varmoiksi.
    """
    observed = int(stats.get("utility_area_observed", 0) or 0)
    snapped = int(stats.get("utility_area_snapped", 0) or 0)
    unnamed = int(stats.get("utility_area_unnamed", 0) or 0)
    without_area = int(stats.get("utility_without_area", 0) or 0)
    lines = [
        _line(
            "Utilityn alue",
            f"{observed} havaittua, {snapped} napsautettua, "
            f"{without_area} ilman aluetta",
        )
    ]
    if unnamed:
        lines.append(
            _line(
                "Nimetön alue",
                f"{unnamed} tapahtumaa (lähin pelaaja löytyi, mutta pelillä "
                "ei ole nimeä hänen alueelleen)",
            )
        )
    return lines


@app.command("classify")
def classify(
    target: str = typer.Argument(
        ...,
        metavar="MAP_DEMO_ID",
        help="Parsitun demon tunniste, sama jolla parse ajettiin.",
    ),
    team: str | None = typer.Option(
        None,
        "--team",
        help=(
            "Subjektijoukkueen kokoonpanotunniste (lineup_key) tai sen "
            "yksikäsitteinen alkuosa. Ilman tätä komento listaa demon "
            "kokoonpanot."
        ),
    ),
    all_teams: bool = typer.Option(
        False,
        "--kaikki-joukkueet",
        help=(
            "Luokittele demo molempien joukkueiden näkökulmasta. Kumpikin saa "
            "oman tuloksensa; --team jätetään huomiotta."
        ),
    ),
    show: bool = typer.Option(
        False,
        "--show",
        help=(
            "Tulosta kierroslista: kierros, puoli, raha ja varustearvo per "
            "pelaaja, loss count, tyyppi ja perustelu."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--pakota",
        help="Luokittele vaikka manifesti täsmäisi.",
    ),
) -> None:
    """Luokittele parsitun demon kierrokset yhden joukkueen näkökulmasta.

    Kirjoittaa ``classified/<team_key>/<map_demo_id>.parquet``-taulun, saman
    sisällön kierroslistana Markdownina ja manifestin. Demoa ei lueta, joten
    kynnysten säätö ja uudelleenajo valmistuvat sekunneissa.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)

    teams: list[str | None]
    if all_teams:
        teams = list(classify_stage.team_keys(archive, target))
    else:
        teams = [team]

    for index, choice in enumerate(teams):
        if index:
            typer.echo("")
        result = classify_stage.run(
            settings.thresholds,
            settings.league,
            archive,
            target,
            choice,
            force=force,
        )
        typer.echo(_render_classify(result))
        if show:
            lines = result.stats.get("rows")
            typer.echo("")
            if lines:
                typer.echo(_render_round_list(lines))
            else:
                typer.echo(
                    "Kierroslistaa ei saatu luettua tuloksesta. Aja komento "
                    "uudelleen lipulla --pakota."
                )


def _render_classify(result: StageResult) -> str:
    """Kokoa ``classify``-komennon yhteenveto.

    Erotettu omaksi funktiokseen, jotta tuloste on testattavissa ilman
    komentorivin ajamista.
    """
    stats = result.stats
    lines: list[str] = []
    lines.append(f"{'Ohitettu' if result.skipped else 'Luokiteltu'}: {result.unit}")

    if result.status != "ok":
        lines.append(_line("Tila", str(result.status)))
    if result.reason:
        lines.append(_line("Syy", result.reason))

    team_key = stats.get("team_key")
    if team_key:
        lines.append(_line("Joukkue", str(team_key)))

    if "unreadable" in stats:
        lines.append(_line("Kierrokset", f"lukuja ei saatu ({stats['unreadable']})"))
        lines.append(_line("Ajoaika", _seconds(result.duration_s)))
        return "\n".join(lines)

    lines.append(_line("Kierrokset", str(int(stats.get("rounds", 0) or 0))))

    distribution = stats.get("by_type") or {}
    if distribution:
        # Kierrostyyppien vakiojärjestys, jotta tuloste on vertailukelpoinen
        # ajosta toiseen; tuntemattomat lopuksi.
        order = [t for t in ROUND_TYPES if t in distribution]
        order += [t for t in sorted(distribution) if t not in ROUND_TYPES]
        lines.append(_line("Tyypit", ", ".join(f"{t} {distribution[t]}" for t in order)))

    # AD-9: luokittelematon kierros ei saa hukkua tyyppijakaumaan, joten se on
    # omalla rivillään -- ja vain siellä.
    unclassified = int(stats.get("unclassified", 0) or 0)
    if unclassified:
        lines.append(
            _line(
                UNCLASSIFIED.capitalize(),
                f"{unclassified} (havainto puuttuu, syy näkyy kierroslistassa)",
            )
        )

    unnumbered = int(stats.get("unnumbered", 0) or 0)
    if unnumbered:
        lines.append(
            _line(
                "Numeroimattomat",
                f"{unnumbered} (ei kierrosnumeroa, jätetty luokittelun "
                "ulkopuolelle)",
            )
        )

    for path in result.outputs:
        lines.append(_line("Tulos", str(path)))
    if result.manifest_path is not None:
        lines.append(_line("Manifesti", str(result.manifest_path)))
    lines.append(_line("Ajoaika", _seconds(result.duration_s)))
    return "\n".join(lines)


#: Perustelu ei mahdu sarakkeeksi, joten se tulostetaan omalle sisennetylle
#: rivilleen -- katkaistu perustelu ei kelpaa, koska juuri sitä vasten
#: luokittelu tarkistetaan demosta.
_REASON_COLUMN = "reason"


def _render_round_list(rows: list[dict]) -> str:
    """Kierroslista konsoliin.

    Tämä on se tuloste, jolla käyttäjä tarkistaa luokittelun demoa vasten:
    jokaisella kierroksella näkyvät sekä päätös että ne arvot, joihin se nojasi.
    Sarakkeet tulevat vaiheen omasta ``ROUND_LIST_COLUMNS``-määrittelystä, joten
    konsoli ja Markdown eivät voi esittää eri asioita.
    """
    if not rows:
        return "Kierroksia ei ole."

    narrow = [
        (index, label)
        for index, (label, key) in enumerate(classify_stage.ROUND_LIST_COLUMNS)
        if key != _REASON_COLUMN
    ]
    reason_index = next(
        index
        for index, (_, key) in enumerate(classify_stage.ROUND_LIST_COLUMNS)
        if key == _REASON_COLUMN
    )

    cells = [classify_stage.round_list_cells(row) for row in rows]
    headers = [label for _, label in narrow]
    widths = [
        max(len(label), *(len(r[index]) for r in cells))
        for index, label in narrow
    ]

    result: list[str] = []
    result.append("  ".join(o.ljust(w) for o, w in zip(headers, widths)).rstrip())
    result.append("  ".join("-" * w for w in widths))
    for row_cells in cells:
        narrow_cells = [row_cells[index] for index, _ in narrow]
        result.append(
            "  ".join(s.ljust(w) for s, w in zip(narrow_cells, widths)).rstrip()
        )
        reason = row_cells[reason_index].strip()
        if reason:
            result.append(f"    {reason}")
    result.append("")
    result.append("Rahaluvut ovat $/pelaaja freezetimen lopussa. Käytössä = jäljellä +")
    result.append("käytetty; jäljellä on saldo ostojen jälkeen, joten")
    result.append("säästökierroksella se on suuri.")
    return "\n".join(result)


def _seconds(value: float) -> str:
    """Sekunnit suomalaisella desimaalipilkulla."""
    return f"{value:.1f} s".replace(".", ",")


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
