"""Komentorivikuori (Typer).

CLI on ohut: se lukee asetukset, valitsee vaiheet ja näyttää tuloksen. Se ei
kutsu adaptereita eikä arkistoa suoraan, eikä siinä ole analyysilogiikkaa --
sama putki ajetaan myöhemmin web-kuoren takaa muuttamatta domainia.

Komentoja on kuusi: ``info`` näyttää asetukset, arkiston tilan ja avainten
tilan paljastamatta avainten arvoja, ``discover`` hakee divisioonan ottelut ja
kirjoittaa niistä ottelu- ja joukkueindeksin, ``parse`` ajaa putken
demovaiheen yhdelle demolle, ``classify`` luokittelee sen kierrokset yhden
joukkueen näkökulmasta, ``aggregate`` kokoaa joukkueen luokitellut kierrokset
yhdeksi ``report.json``-tiedostoksi ja ``report`` kirjoittaa siitä luettavan
Markdown-raportin. Loput (``select``, ``fetch``, ``scout``, ``next``,
``collect``, ``import``) tulevat myöhemmissä storyissa.

Arkistoon ja adaptereihin ei kosketa täältä: polut pyydetään
``stages.archive_paths``ilta, demoportti ``stages.parse.default_parser``ilta ja
otteluportti ``stages.discover.default_source``ilta. Riippuvuusnuoli on
``cli -> stages -> {domain, adapters, archive}``.

Käyttäjä ei koodaa itse, joten mikään virhe ei saa päätyä ruudulle raakana
pinojälkenä: :func:`main` muuntaa ne suomenkielisiksi viesteiksi ja
paluukoodeiksi.
"""

from __future__ import annotations

import sys

import typer

from pappascout import __version__
from pappascout.constants import (
    ROUND_TYPES,
    SAMPLE_BUCKET_FI,
    SAMPLE_BUCKETS,
    UNCLASSIFIED,
)
from pappascout.domain.models import Settings, load_settings, secrets_env_path
from pappascout.errors import PappascoutError
from pappascout.render.view import players_text
from pappascout.stages import StageResult, archive_paths
from pappascout.stages import aggregate as aggregate_stage
from pappascout.stages import classify as classify_stage
from pappascout.stages import discover as discover_stage
from pappascout.stages import parse as parse_stage
from pappascout.stages import render as render_stage

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

#: Montako pelaajaa luetellaan nimeltä yhdessä yhteenvedon rivissä.
#: Loput lasketaan; koko luettelo on aina indeksitiedostossa.
MAX_LISTED_PLAYERS = 5


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


def _pruning_value(value: object) -> str:
    """Karsinta-asetuksen arvo ``info``-tulosteeseen.

    Sama muoto kuin raportin yhteenvedossa
    (:func:`pappascout.render.view._pruning_summary_text`): tyhjä lista on
    "ei yhtään" eikä tyhjä merkkijono, ja totuusarvo on suomeksi. Kaksi
    tulostetta samasta osiosta eri sanoilla lukisi kuin arvot olisivat eri.
    """
    if isinstance(value, bool):
        return "kyllä" if value else "ei"
    if isinstance(value, list):
        return "/".join(f"{item:g}".replace(".", ",") for item in value) or "ei yhtään"
    return str(value)


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
    # Karsintasäännöt (Story 2.13). Rivi on mekaaninen luettelo osion
    # kentistä, joten kuudes sääntö näkyy heti kun se on osiossa -- ja se on
    # tässä samasta syystä kuin raportin yhteenvedossa: sääntö, joka ei osu
    # kertaakaan, ei näy raportissa mitenkään, joten ilman tätä riviä
    # käyttäjä ei näe mistään, mitkä säännöt ovat päällä.
    lines.append(
        "  Karsinta           "
        + ", ".join(
            f"{key} {_pruning_value(value)}"
            for key, value in sorted(settings.report.model_dump(mode="json").items())
        )
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


@app.command("discover")
def discover(
    team: str | None = typer.Option(
        None,
        "--team",
        help=(
            "Joukkueen nimi, sen yksikäsitteinen osa tai joukkuetunniste. "
            "Kirjainkoolla ei ole väliä. Monitulkintainen nimi listaa "
            "vaihtoehdot eikä valitse mitään. Ilman tätä indeksit "
            "kirjoitetaan eikä joukkuetta haeta."
        ),
    ),
) -> None:
    """Hae divisioonan ottelut ja kirjoita ottelu- ja joukkueindeksi.

    Yksi verkkokutsu per kilpailu riittää: ottelurivillä ovat molempien
    joukkueiden aloittajat ja vaihtopelaajat. Vakirosteri on niiden yhdiste
    joukkueen kaikista otteluista -- myös pelaamattomista, joten yksi pelattu
    ottelu yhdestätoista ei tee rosterista vajaata.

    Komento hakee ottelulistan **joka kerta uudelleen**: sitä ei välimuistiteta
    eikä ajoa ohiteta, koska uusien otteluiden näkeminen on koko komennon
    tarkoitus. Siksi tässä ei ole --pakota-valintaa.

    Arkiston hakemistoja ei nimetä uudelleen. Yhteys arkistoon näkyy
    index/teams.json:in lineup_keys-kentässä.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    typer.echo("Haetaan divisioonan otteluita...", err=True)
    result = discover_stage.run(
        settings.league,
        archive,
        team,
        source=discover_stage.default_source(settings, archive),
        thresholds=settings.thresholds,
    )
    typer.echo(_render_discover(result))


def _render_discover(result: StageResult) -> str:
    """Kokoa ``discover``-komennon yhteenveto.

    Tärkein rivi on joukkueiden ja rosterien laajuus: käyttäjä tarkistaa siitä,
    näkyykö koko divisioona. Rosterien vaihteluväli on mukana, koska liian pieni
    rosteri on ainoa tapa huomata puuttuva ``substitutes``-lista avaamatta
    tiedostoa.

    Ilman ``--team``-valintaa tuloste **luettelee divisioonan joukkueet**.
    Ilman sitä nimet näkisi vain syöttämällä tahallaan tuntemattoman nimen ja
    lukemalla ne virheilmoituksesta -- eli juuri sitä, mitä käyttäjä tarvitsee
    monitulkintaisen haun jälkeen, ei saisi ilman virhettä.
    """
    stats = result.stats
    matches_found = int(stats.get("matches", 0) or 0)
    teams_found = int(stats.get("teams", 0) or 0)
    roster_min = int(stats.get("roster_min", 0) or 0)
    roster_max = int(stats.get("roster_max", 0) or 0)
    span = (
        f"{roster_min} pelaajaa"
        if roster_min == roster_max
        else f"{roster_min}-{roster_max} pelaajaa"
    )

    lines: list[str] = []
    lines.append(
        f"Divisioona haettu: {teams_found} joukkuetta, {matches_found} ottelua"
    )
    if result.reason:
        lines.append(_line("Huomio", result.reason))
    lines.append(
        _line(
            "Pelatut ottelut",
            f"{int(stats.get('matches_played', 0) or 0)} / {matches_found}",
        )
    )
    lines.append(_line("Rosterit", span))
    lines.extend(_discover_gaps(stats))

    team = stats.get("team")
    if isinstance(team, dict):
        lines.extend(_discover_team(team))
    else:
        lines.extend(_discover_division(stats))

    lines.append("")
    for path in result.outputs:
        lines.append(_line("Tulos", str(path)))
    lines.append(_line("Ajoaika", _seconds(result.duration_s)))
    return "\n".join(lines)


def _discover_gaps(stats: dict) -> list[str]:
    """Rivit siitä, mikä jäi pois -- pudotukset, kiistat ja siirtymät.

    Jokainen näistä on hiljainen pudotus, jos sitä ei sanota ääneen: rosteri
    olisi vain lyhyempi tai pidempi kuin pitäisi, eikä mikään kertoisi miksi.
    """
    lines: list[str] = []
    without_id = int(stats.get("players_without_steam_id", 0) or 0)
    if without_id:
        dropped = stats.get("dropped_players") or []
        named = ", ".join(
            str(row.get("nickname") or row.get("player_id") or "?")
            for row in dropped[:MAX_LISTED_PLAYERS]
        )
        if len(dropped) > MAX_LISTED_PLAYERS:
            named += f" (+{len(dropped) - MAX_LISTED_PLAYERS} muuta)"
        lines.append(
            _line(
                "Ilman SteamID64:aa",
                f"{without_id} pelaajaa jäi pois rostereista: {named}",
            )
        )
    rows_without_id = int(stats.get("team_rows_without_id", 0) or 0)
    if rows_without_id:
        lines.append(
            _line(
                "Tunnisteettomat joukkuerivit",
                f"{rows_without_id} kpl ohitettiin -- niitä ei voi liittää "
                "yhteenkään joukkueeseen",
            )
        )
    transfers = stats.get("transfers") or []
    moved = [t for t in transfers if t.get("kind") == "released"]
    shared = [t for t in transfers if t.get("kind") == "shared"]
    if moved:
        lines.append(
            _line(
                "Siirtyneet pelaajat",
                ", ".join(
                    f"{t.get('nickname') or t.get('game_player_id')} "
                    f"({t.get('from_team')})"
                    for t in moved[:MAX_LISTED_PLAYERS]
                ),
            )
        )
    if shared:
        lines.append(
            _line(
                "Kahdessa joukkueessa",
                ", ".join(
                    f"{t.get('nickname') or t.get('game_player_id')} "
                    f"({t.get('from_team')})"
                    for t in shared[:MAX_LISTED_PLAYERS]
                ),
            )
        )
    contested = stats.get("contested_lineup_keys") or []
    if contested:
        lines.append(
            _line(
                "Kiistanalaiset kokoonpanot",
                ", ".join(str(key) for key in contested)
                + " -- useampi joukkue ylittää kynnyksen",
            )
        )
    return lines


def _discover_division(stats: dict) -> list[str]:
    """Divisioonan joukkueet luettelona, tunnisteineen."""
    division = stats.get("division") or []
    if not division:
        return []
    lines = ["", "Divisioonan joukkueet:"]
    for team in division:
        name = str(team.get("name") or team.get("team_key") or "")
        lines.append(
            f"  {name} -- {int(team.get('roster_size', 0) or 0)} pelaajaa, "
            f"tunniste {team.get('team_key')}"
        )
    return lines


def _discover_team(team: dict) -> list[str]:
    """Haetun joukkueen rivit."""
    lines = ["", f"Joukkue: {team.get('name') or team.get('team_key') or ''}"]
    lines.append(_line("Tunniste", str(team.get("team_key", ""))))
    faction_ids = [str(key) for key in team.get("faction_ids") or []]
    if len(faction_ids) > 1:
        lines.append(
            _line(
                "Lähteen tunnisteet",
                ", ".join(faction_ids) + " -- sama joukkue, eri kaudet",
            )
        )
    roster = [str(player) for player in team.get("roster") or []]
    lines.append(
        _line(
            "Vakirosteri",
            f"{len(roster)} pelaajaa: {', '.join(roster)}"
            if roster
            else "ei yhtään pelaajaa",
        )
    )
    released = [str(player) for player in team.get("released") or []]
    if released:
        lines.append(
            _line("Siirtyneet pois", ", ".join(released))
        )
    shared = [str(player) for player in team.get("shared_players") or []]
    if shared:
        lines.append(
            _line(
                "Myös toisessa joukkueessa",
                f"{len(shared)} pelaajaa -- kiistaa ei ratkaistu",
            )
        )
    lines.append(
        _line(
            "Ottelut",
            f"{int(team.get('matches', 0) or 0)} kpl, joista pelattu "
            f"{int(team.get('matches_played', 0) or 0)}",
        )
    )
    lineups = [str(key) for key in team.get("lineup_keys") or []]
    if lineups:
        lines.append(_line("Arkiston kokoonpanot", ", ".join(lineups)))
    alternatives = [str(other) for other in team.get("alternative_names") or []]
    if alternatives:
        lines.append(_line("Muut havaitut nimet", ", ".join(alternatives)))
    return lines


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
    """Parsi demo kuudeksi tauluksi.

    Kirjoittaa ``parsed/<map_demo_id>/rounds.parquet``-, ``ticks.parquet``-,
    ``events.parquet``-, ``lineups.parquet``-, ``deaths.parquet``- ja
    ``callouts.parquet``-taulut sekä niiden manifestin.
    Jos manifesti täsmää, vaihe ohitetaan eikä demoa lueta uudelleen.
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


#: Tulosteen sarakeleveys, jotta arvot linjautuvat otsikoiden alle. Pisin
#: otsikko on "Tuntemattomat esineet" (21 merkkiä), ja arvo tarvitsee vähintään
#: yhden välilyönnin eteensä.
_PARSE_LABEL_WIDTH = 22


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
                f"{skipped_rounds} (warmup ja puukkokierros)",
            )
        )

    lines.extend(_match_restarts(stats))

    no_anchor = int(stats.get("no_freeze_end", 0) or 0)
    if no_anchor:
        lines.append(
            _line(
                "Ilman ankkuria",
                f"{no_anchor} (freezetime-tick puuttuu, kierros silti mukana)",
            )
        )

    lines.extend(_buy_window(stats))
    lines.extend(_armed_players(stats))
    lines.extend(_armored_players(stats))
    lines.extend(_sample_points(stats, rounds))
    lines.extend(_utility(stats, rounds))
    lines.extend(_callouts(stats))
    lines.extend(_map_name(stats))
    lines.extend(_deaths(stats, rounds))
    lines.extend(_lineups(stats))

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


#: Montako kierrosnumeroa tulostetaan, kun ostoja jäi katkaisun taakse.
#: Katkaisu osuu noin puoleen kierroksista, joten pahimmillaan lista olisi koko
#: ottelun mittainen -- ja juuri silloin käyttäjän pitäisi nähdä yhdellä
#: silmäyksellä, että kyse on systemaattisesta viasta eikä yhdestä kierroksesta.
_MAX_LOST_PURCHASE_ROUNDS = 10


def _buy_window(stats: dict) -> list[str]:
    """Ostoikkunan rivit ``parse``-tulosteeseen.

    Kolme asiaa, jotka on sanottava ääneen:

    **Mistä hetkestä luvut on luettu.** Talous mitataan ostoajan lopusta eikä
    freezetimen lopusta, ja mittaushetki on asetus. Ilman riviä kaksi eri
    asetuksella ajettua tulosta näyttäisivät samalta. Rivi kertoo myös
    mittaushetkien todellisen jakauman: asetus lupaa ikkunan pituuden, mutta
    kuoleman katkaisu vetää mediaanin usein sen alle.

    **Maksoiko kuoleman katkaisu jotain.** Kuolema katkaisee ikkunan noin
    puolella kierroksista -- se on normaali polku eikä poikkeus, joten pelkkä
    katkaisujen määrä ei ole hälytys. Hälytys on
    ``buy_window_purchases_after_cut``: jos joku osti vielä katkaisun jälkeen,
    mittaus menetti ostoksen. Sen kuuluu olla nolla, ja **nolla sanotaan
    ääneen** -- vaiettu nolla ei erottuisi vaietusta viidestä. Nollalla on
    kaksi eri syytä, joten tarkistamatta jääneet katkaisut sanotaan erikseen.

    **Mitä mittauspisteessä meni pieleen.** Tyhjä tick, kadonneet pelaajat,
    kokonaan tyhjä joukkuerivi ja palautuksen jättämä vanhentunut varustearvo
    saavat kukin rivinsä vain kun luku on nollasta poikkeava: ne ovat vikoja
    eivätkä normaalia, joten nollan toistaminen hukuttaisi ne.

    Ohitetussa ajossa rivit jätetään pois: mittauspiste on tauluissa
    (``buy_end_tick``), mutta katkaisujen ja menetettyjen ostosten määrää ei
    voi lukea valmiista tuloksesta.
    """
    if "buy_window_truncated_by_death" not in stats:
        return []

    lines: list[str] = [_line("Mittauspiste", _measurement_point(stats))]

    truncated = int(stats.get("buy_window_truncated_by_death", 0) or 0)
    missed = int(stats.get("buy_window_purchases_after_cut", 0) or 0)
    unchecked = int(stats.get("buy_window_cuts_unchecked", 0) or 0)

    if truncated:
        cut = (
            f"{_rounds_fi(truncated)} mitattiin aiemmin, koska kierroksen "
            "ensimmäinen kuolema katkaisi ikkunan"
        )
    else:
        cut = "ei yhtään -- ikkuna ylsi loppuun joka kierroksella"
    if missed:
        cut += f"; {_players_fi(missed)} osti vielä katkaisun jälkeen"
        rounds = tuple(stats.get("buy_window_rounds_with_lost_purchases") or ())
        if rounds:
            shown = rounds[:_MAX_LOST_PURCHASE_ROUNDS]
            listed = ", ".join(str(number) for number in shown)
            if len(rounds) > len(shown):
                listed += f" (+{len(rounds) - len(shown)} muuta)"
            cut += f" -- kierrokset (round_raw) {listed}"
        else:
            cut += " -- ostoja jäi mittauksen taakse"
    else:
        cut += "; yksikään osto ei jäänyt katkaisun taakse"
    if unchecked:
        cut += (
            f"; {_rounds_fi(unchecked)} ei voitu tarkistaa lainkaan "
            "(ikkunan lopun tickiltä ei saatu pelaajia)"
        )
    lines.append(_line("Kuoleman katkaisu", cut))

    lines.extend(_buy_window_faults(stats))
    return lines


def _measurement_point(stats: dict) -> str:
    """Rivin ``Mittauspiste`` teksti: mistä hetkestä talousluvut on luettu.

    Kolme tilaa on pidettävä erillään. ``buy_window_seconds`` puuttuu tai on
    ``None``, kun portti ei kerro ikkunaa -- silloin mittaushetkeä ei tiedetä
    eikä sitä saa väittää. Nolla tarkoittaa ankkuria, ja se sanotaan
    freezetimen lopuksi: "ostoajan loppu, ikkuna 0,0 s" olisi totta mutta
    harhaanjohtavaa, koska juuri sen niminen mittaus oli tämän tarinan vika.
    Positiivinen arvo on ostoajan loppu.
    """
    window = stats.get("buy_window_seconds")
    if window is None:
        return "ei tiedossa (demoportti ei kerro ostoikkunan pituutta)"

    window = float(window)
    if not window:
        return "talous luettu freezetimen lopusta (ikkuna 0,0 s)"

    text = f"talous luettu ostoajan lopusta (ikkuna {_seconds(window)}"
    # Ikkuna lasketaan tickratesta. Jos sitä ei saatu mitattua, myös sekunnit
    # ovat oletuksen varassa -- ja rivi, joka tulostaa sekunnit yhtä varmasti
    # kummassakin tapauksessa, väittäisi mittausta.
    if "tick_rate" in stats and not stats.get("tick_rate_measured", True):
        text += ", tickrate oletus"
    text += ")"

    offsets = stats.get("buy_end_offsets_s")
    if offsets:
        low, middle, high = (float(value) for value in offsets)
        text += (
            f"; mitattu {_seconds(low)}-{_seconds(high)} ankkurista, "
            f"mediaani {_seconds(middle)}"
        )
    return text


def _buy_window_faults(stats: dict) -> list[str]:
    """Mittauspisteen viat omina riveinään -- vain kun luku on nollasta poikkeava.

    Nämä neljä ovat kaikki **vikoja eivätkä havaintoja**, toisin kuin kuoleman
    katkaisu. Nollan toistaminen joka ajossa opettaisi lukijan ohittamaan ne.
    """
    lines: list[str] = []
    empty = int(stats.get("buy_window_ticks_without_players", 0) or 0)
    if empty:
        lines.append(
            _line(
                "Ostoajan tick tyhjä",
                f"{_rounds_fi(empty)} -- tickiltä ei saatu pelaajia, mittaus "
                "palautui freezetimen ankkuriin",
            )
        )

    lost = int(stats.get("buy_window_players_lost", 0) or 0)
    sides = int(stats.get("buy_window_sides_without_rows", 0) or 0)
    if lost:
        text = (
            f"{_players_fi(lost)} oli luettavissa ankkurilla mutta ei enää "
            "mittauspisteessä"
        )
        if sides:
            text += (
                f"; {sides} joukkueriviä jäi kokonaan tyhjäksi, eikä niitä "
                "luokitella"
            )
        lines.append(_line("Kadonneet pelaajat", text))

    stale = int(stats.get("buy_window_stale_equipment", 0) or 0)
    if stale:
        lines.append(
            _line(
                "Vanhentunut arvo",
                f"{_players_fi(stale)}: arvo nousi ilman ostosta -- "
                "palautetun ostoksen jälki, enintään 1000 $/pelaaja",
            )
        )
    return lines


def _rounds_fi(count: int) -> str:
    """``1 kierros`` / ``13 kierrosta`` -- suomen partitiivi taipuu luvulla 1."""
    return f"{count} kierros" if count == 1 else f"{count} kierrosta"


def _players_fi(count: int) -> str:
    """``1 pelaaja`` / ``2 pelaajaa``.

    Yksi menetetty ostos on juuri se tapaus, jota rivi on kertomassa, joten
    "1 pelaajaa" olisi väärin juuri silloin kun rivi eniten merkitsee.
    """
    return f"{count} pelaaja" if count == 1 else f"{count} pelaajaa"


#: Sääntö, jolla ``players_armed_buy_end`` lasketaan. Tulostetaan aina
#: jakauman kanssa: ilman sitä lukuja ei voi tulkita, ja rivin koko tarkoitus
#: on olla itsetarkistus ajon yhteydessä.
_ARMED_RULE = "panssari ja ase hallussa ostoajan lopussa"

#: Sääntö, jolla ``players_armored_buy_end`` lasketaan. Sama peruste kuin
#: yllä, ja lisäksi yksi: kaksi lähes samannimistä riviä peräkkäin luetaan
#: väärin ilman sääntöä kummankin perässä. **Hallussa eikä ostettu**, kuten
#: ylemmälläkin -- panssari säilyy kierroksen yli hengissä selvinneellä.
_ARMORED_RULE = "panssari hallussa ostoajan lopussa, aseesta riippumatta"

#: Montako tuntematonta nimeä tulostetaan enintään. Jos demoparser2 muuttaa
#: nimeämistapaansa, **jokainen** nimi on tuntematon: ilman katkaisua rivi
#: olisi satojen nimien mittainen juuri silloin, kun käyttäjän pitäisi nähdä
#: yhdellä silmäyksellä että jokin on pahasti pielessä.
_MAX_UNKNOWN_ITEMS = 20


def _player_counter_line(
    label: str, rule: str, distribution: dict | None, missing: int
) -> list[str]:
    """Yhden pelaajalaskurin arvojakauma yhtenä tulosteriviä.

    Jaettu aseistettujen ja panssaroitujen kesken samasta syystä kuin
    ``stages.parse``in ``_column_distribution``: rivien **ero** on se, mitä
    lukija tulosteesta lukee, ja kaksi kopiota latoisi ne ennen pitkää eri
    tavalla -- toinen kertoisi puuttuvista havainnoista ja toinen vaikenisi.

    **Jakauma eikä ääripäät**: 41 riviä nollaa ja yksi viitonen antaisi
    ``0-5``, joka näyttää terveeltä, mutta ``0 -> 41, 5 -> 1`` ei.

    Args:
        label: Rivin otsikko.
        rule: Sääntö, jolla luku on laskettu. Aina mukana: kaksi lähes
            samannimistä riviä peräkkäin luetaan väärin ilman sitä.
        distribution: Arvo -> rivien määrä, tai ``None`` jos lukua ei ole
            (ohitettu ajo vanhalla portilla). Rivi jätetään silloin pois.
        missing: Rivit, joilta havainto puuttuu.

    Returns:
        Nolla tai yksi riviä.
    """
    if distribution is None:
        return []
    prefix = f"{rule}; "
    if not distribution:
        return [_line(label, f"{prefix}ei yhtään havaintoa ({missing} riviä)")]
    spread = ", ".join(
        f"{value} -> {rows} riviä" for value, rows in sorted(distribution.items())
    )
    text = f"{prefix}{spread}"
    if missing:
        text += f"; havainto puuttuu {missing} riviltä"
    return [_line(label, text)]


def _armed_players(stats: dict) -> list[str]:
    """Kalustolaskurin arvojakauma ``parse``-tulosteeseen.

    Laskuri on havainto, jonka voi tarkistaa vain katsomalla: väärä sääntö
    tuottaisi taulun, joka läpäisee jokaisen skeematarkistuksen.

    Tuntemattomat tavaraluettelon nimet saavat oman rivinsä. Ne eivät aseista
    ketään (luokittelu on sallittujen aseiden luettelo), joten ilman riviä uusi
    ase ja uusi veitsiskini näyttäisivät täsmälleen samalta: jakauma vain
    valuisi hiljaa alaspäin.
    """
    return _player_counter_line(
        "Aseistettuja",
        _ARMED_RULE,
        stats.get("armed_distribution"),
        int(stats.get("armed_missing", 0) or 0),
    ) + _armed_unknown_items(stats)


def _armored_players(stats: dict) -> list[str]:
    """Panssarilaskurin arvojakauma ``parse``-tulosteeseen.

    Oma rivinsä aseistettujen rivin alla, ei sen jatke. Ne ovat eri havaintoja
    ja eroavat eniten pistoolikierroksella, jolla aseistettuja on käytännössä
    0: juuri siitä erosta tavoiteanalyysin *"5 kevlaria"* luetaan. Yhdistetty
    rivi peittäisi eron, jonka takia laskureita on kaksi.

    Rivi toimii myös itsetarkistuksena ajon yhteydessä: jos jakauma on
    identtinen aseistettujen jakauman kanssa, panssarilaskuri lukee väärää
    ehtoa -- ja se näkyy tässä eikä vasta raportissa.

    Tuntemattomia esineitä ei tulosteta tässä: panssarilaskuri ei lue
    tavaraluetteloa, joten esinenimet eivät voi vaikuttaa siihen.
    """
    return _player_counter_line(
        "Panssaroituja",
        _ARMORED_RULE,
        stats.get("armored_distribution"),
        int(stats.get("armored_missing", 0) or 0),
    )


def _match_restarts(stats: dict) -> list[str]:
    """Ottelun uudelleenaloitukset ``parse``-tulosteeseen.

    Uudelleenaloitus ei ole kierros: se ei päädy tauluun lainkaan, joten se
    **ei sisälly** ohitettujen kierrosten lukuun. Oma rivinsä siis, muuten
    kaksi riviä laskisi saman asian tai pudotus jäisi hiljaiseksi.

    Kolme tilaa pidetään erillään samoin kuin :func:`_armed_unknown_items`issa:

    ``match_restarts`` puuttuu
        Ohitettu ajo. Uudelleenaloitus ei ole tauluissa, joten sen määrää ei
        voi lukea valmiista tuloksesta. Rivi jätetään pois kokonaan.
    arvo on ``None``
        Tuore ajo portilla, joka ei raportoi uudelleenaloituksia. "Ei yhtään"
        olisi väite, jota mikään ei tue.
    arvo on luku
        Tuore ajo, jossa portti kertoi luvun. Myös nolla sanotaan ääneen.
    """
    if "match_restarts" not in stats:
        return []
    value = stats.get("match_restarts")
    if value is None:
        return [
            _line(
                "Uudelleenaloitukset",
                "ei tiedossa (demoportti ei raportoi uudelleenaloituksia)",
            )
        ]
    count = int(value)
    if not count:
        return [_line("Uudelleenaloitukset", "ei yhtään")]
    # Yksikkö taipuu: "1 kierrosraja", "2 kierrosrajaa".
    boundaries = "kierrosraja" if count == 1 else "kierrosrajaa"
    return [
        _line(
            "Uudelleenaloitukset",
            f"{count} {boundaries} ilman demon omaa numeroa "
            "-- ei kierros, ei riviä tauluun",
        )
    ]


def _armed_unknown_items(stats: dict) -> list[str]:
    """Tavaraluettelon nimet, joita aseluokittelu ei tunne.

    Kolme eri tilaa, jotka on pidettävä erillään:

    ``armed_unknown_items`` puuttuu
        Ohitettu ajo. Nimiä ei ole taulussa -- ne eivät aseista ketään --
        joten niitä ei voi lukea takaisin. Rivi jätetään pois kokonaan.
    arvo on ``None``
        Tuore ajo portilla, joka ei raportoi tuntemattomia. Rivi sanoo sen
        ääneen: "ei yhtään" olisi väite, jota mikään ei tue.
    arvo on tyhjä
        Tuore ajo, jossa jokainen nimi tunnistettiin. Se on terve tulos, ja
        se sanotaan ääneen.

    Esiintymämäärä tulostetaan nimen perässä (``Uusi Ase x3``): se erottaa
    yhden eksoottisen veitsen demoparser2:n nimeämismuutoksesta, joka osuu
    joka riviin.
    """
    if "armed_unknown_items" not in stats:
        return []
    items = stats.get("armed_unknown_items")
    if items is None:
        return [
            _line(
                "Tuntemattomat esineet",
                "ei tiedossa (demoportti ei raportoi tuntemattomia nimiä)",
            )
        ]
    items = tuple(items)
    if not items:
        return [_line("Tuntemattomat esineet", "ei yhtään")]

    shown = items[:_MAX_UNKNOWN_ITEMS]
    listed = ", ".join(_unknown_item(item) for item in shown)
    hidden = len(items) - len(shown)
    if hidden:
        listed += f" (+{hidden} muuta)"
    count = (
        "1 esinenimi" if len(items) == 1 else f"{len(items)} eri esinenimeä"
    )
    return [
        _line(
            "Tuntemattomat esineet",
            f"{count}: {listed} "
            "(ei laskettu aseeksi -- lisää tunnistettu ase constants.py:hyn)",
        )
    ]


def _unknown_item(item: object) -> str:
    """Muotoile yksi tuntematon nimi esiintymämäärineen."""
    if isinstance(item, (tuple, list)) and len(item) == 2:
        name, seen = item
        return f"{name} x{int(seen)}"
    return str(item)


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

    # Kierros ilman yhtään näytepistettä voi johtua neljästä syystä: ankkuri
    # puuttuu, kierros ratkesi ennen ensimmäistä näytepistettä, näytepisteajat
    # ovat väärin, tai jokainen pelaajarivi oli pawniton (Story 2.10) --
    # viimeinen näkyy omalla rivillään alempana. Erotus kerrotaan, syytä ei
    # arvata; neljäs mainitaan vain kun se on mitattu, jotta selitys ei
    # luettele syytä jota tässä ajossa ei ollut.
    without_samples = rounds - sampled_rounds
    if without_samples > 0:
        reason = "ankkuri puuttuu tai kierros ratkesi ennen ensimmäistä näytepistettä"
        if int(stats.get("sample_points_without_pawn") or 0):
            reason += "; ks. myös Pawniton pelaaja"
        lines.append(
            _line("Ilman näytepistettä", f"{without_samples} kierrosta ({reason})")
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
    #
    # Kaksi riviä, jotka kertovat osin samasta tapahtumasta: pawniton pelaaja
    # on **yksi syy** vajaaseen näytepisteeseen. Ne eivät silti yhdisty
    # yhdeksi luvuksi -- vajaita pisteitä on muistakin syistä, ja pawnittomia
    # rivejä on myös heittotickeiltä, jotka eivät ole näytepisteitä lainkaan.
    # Kytkentä sanotaan siis ääneen sen sijaan että lukija laskisi saman
    # tapahtuman kahdesti.
    without_pawn = int(stats.get("sample_rows_without_pawn") or 0)
    partial = int(stats.get("partial_samples", 0) or 0)
    if partial:
        cause = " -- pawnittomat rivit alla ovat yksi syy" if without_pawn else ""
        lines.append(
            _line(
                "Vajaat näytepisteet",
                f"{partial} (pelaajia vähemmän kuin täydellä pisteellä{cause})",
            )
        )
    lines.extend(_pawnless(stats))
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


def _pawnless(stats: dict) -> list[str]:
    """Pawnittomat rivit ``parse``-tulosteeseen (Story 2.10).

    Pawniton pelaaja on **havainto eikä vika**: hänen kontrollerinsa on
    tallella mutta hahmoaan ei ole kartalla, joten rivi ohitetaan kuten
    katsojan. Se pienentää silti sen kierroksen asetelmaa, ja ilman omaa
    riviään kierros näyttäisi siltä, että joukkue vain pelasi vajaalla.

    Kolme tilaa pidetään erillään samoin kuin
    :func:`_match_restarts`issa, ja tässä ero on koko rivin olemassaolon syy:

    avain puuttuu
        Ohitettu ajo. Riviä ei ole taulussa eikä pistettä sen
        näytepisteissä, joten lukua ei voi lukea valmiista tuloksesta. Rivi
        jätetään pois kokonaan.
    arvo on ``None``
        Tuore ajo portilla, joka ei raportoi pawnittomia rivejä. Rivi sanoo
        sen ääneen -- "ei yhtään" olisi väite, jota mikään ei tue.
    arvo on nolla
        Tuore ajo, jossa jokaisella pelaajalla oli hahmo. Terve tulos, ja
        rivi jätetään pois kuten muillakin poikkeamalaskureilla.

    **Kokonaan väliin jäänyt näytepiste** saa saman rivin jatkoksi eikä omaa
    riviään: se on saman ilmiön vakavampi muoto, ja erilliset rivit
    houkuttelisivat lukemaan ne kahdeksi eri tapahtumaksi.
    """
    if "sample_rows_without_pawn" not in stats:
        return []
    rows = stats.get("sample_rows_without_pawn")
    if rows is None:
        return [
            _line(
                "Pawniton pelaaja",
                "ei tiedossa (demoportti ei raportoi pawnittomia rivejä)",
            )
        ]
    count = int(rows)
    if not count:
        return []
    value = (
        f"{count} riviä ohitettiin (kontrolleri tallella, hahmoa ei kartalla)"
    )
    dropped = int(stats.get("sample_points_without_pawn") or 0)
    if dropped:
        value += f"; {dropped} näytepistettä jäi kokonaan väliin"
    return [_line("Pawniton pelaaja", value)]


def _deaths(stats: dict, rounds: int) -> list[str]:
    """Kuolemataulun rivit ``parse``-tulosteeseen (Story 2.7).

    Kolme kysymystä, joihin tuloste vastaa:

    **Syntyikö aineistoa.** Rivimäärä ja se, monellako kierroksella kuoltiin.
    Jälkimmäinen on mukana siksi, että pelkkä rivimäärä ei paljastaisi, jos
    kaikki kuolemat kasautuisivat yhdelle kierrokselle.

    **Katosiko jotain.** Numeroimattomat kierrokset (puukkokierros), tickitön
    kuolema, rajojen ulkopuoliset kuolemat, uhriton tapahtuma ja puoleton uhri
    ovat eri syitä eivätkä saa niputtua yhdeksi. Ensimmäinen on **odotettu**
    liigademossa -- siellä puukkokierroksella kuollaan -- ja loput ovat
    nollia tavoitetilassa.

    **Onko havainto ehjä.** Ampujaton kuolema on havainto (putoaminen,
    pommi), puuttuva alue ei ole. Ne ovat siksi eri riveillä: yhteinen luku
    näyttäisi aluevialta, jota ei ole.
    """
    if "deaths_unreadable" in stats:
        return [_line("Kuolemat", f"lukuja ei saatu ({stats['deaths_unreadable']})")]
    if "death_rows" not in stats:
        return []

    rows = int(stats["death_rows"])
    death_rounds = int(stats.get("death_rounds", 0) or 0)
    lines = [_line("Kuolemat", f"{rows} ({death_rounds}/{rounds} kierroksella)")]

    # Puukkokierroksen kuolemat: odotettu luku, ei vika. Ilman sitä pudotus
    # olisi hiljainen -- ja juuri se pudotus on tämän taulun ainoa
    # puukkokierrossääntö.
    unnumbered = int(stats.get("deaths_unnumbered_rounds", 0) or 0)
    if unnumbered:
        lines.append(
            _line(
                "Numeroimattomilta",
                f"{unnumbered} kuolemaa (warmup ja puukkokierros)",
            )
        )

    without_attacker = int(stats.get("deaths_without_attacker", 0) or 0)
    if without_attacker:
        lines.append(
            _line(
                "Ampujaton kuolema",
                f"{without_attacker} (putoaminen tai pommi; havainto eikä vika)",
            )
        )

    for key, label in (
        ("deaths_without_victim_area", "Uhri ilman aluetta"),
        ("deaths_without_attacker_area", "Ampuja ilman aluetta"),
    ):
        count = int(stats.get(key, 0) or 0)
        if count:
            lines.append(
                _line(label, f"{count} riviä (koordinaatit silti tallessa)")
            )

    for key, label, detail in (
        (
            "deaths_without_tick",
            "Kuolema ilman tickiä",
            "rivi pudotettiin: ilman tickiä kierrosta eikä t_s:ää ole",
        ),
        (
            "deaths_outside_rounds",
            "Kierrosten välissä",
            "ei kuulu millekään kierrokselle, joten t_s:ää ei ole",
        ),
        (
            "deaths_without_victim",
            "Kuolema ilman uhria",
            "rivi pudotettiin: tapahtumalta puuttui user_steamid",
        ),
        (
            "deaths_without_victim_side",
            "Uhri ilman puolta",
            "rivi pudotettiin: kuolema ei kuulu kummallekaan joukkueelle",
        ),
        (
            "deaths_attacker_without_side",
            "Ampuja ilman puolta",
            "rivi säilyi, ampujan kokoonpano jäi tyhjäksi",
        ),
    ):
        count = int(stats.get(key, 0) or 0)
        if count:
            lines.append(_line(label, f"{count} ({detail})"))

    return lines


def _lineups(stats: dict) -> list[str]:
    """Kokoonpanotaulun rivit ``parse``-tulosteeseen (Story 2.6).

    Rivi **per kokoonpano**, ei yhteislukuja: demo sisältää molempien
    joukkueiden pelaajat, joten yhteinen klaaniluettelo on epätyhjä heti kun
    vastustajalla on nimi eikä siis kerro subjektijoukkueesta mitään. Sama
    koskee nimettömiä pelaajia.

    Jokaisella rivillä on ``lineup_key``, koska käyttäjän seuraava komento on
    ``classify --team <lineup_key>``: nimi kertoo kenestä on kyse, tunniste
    kertoo mitä komentoriville kirjoitetaan.
    """
    if "lineups_unreadable" in stats:
        return [
            _line("Kokoonpanot", f"lukuja ei saatu ({stats['lineups_unreadable']})")
        ]
    if "lineup_rows" not in stats:
        return []

    # Avain on tarkistettu yllä, joten oletusarvoa ei tarvita: se peittäisi
    # tuottajan ja kuluttajan erkaantumisen nollana.
    rows = int(stats["lineup_rows"])
    lineups = tuple(stats.get("lineups") or ())
    lines = [_line("Kokoonpanot", f"{rows} pelaajariviä")]
    for key, clan, players, without_name in lineups:
        name = str(clan) if clan else "klaaninimeä ei havaittu"
        detail = f"{name} ({key}) -- {int(players)} pelaajaa"
        if int(without_name):
            detail += (
                f", {int(without_name)} ilman nimeä (raportti näyttää heille "
                "SteamID64:n)"
            )
        lines.append(_line("Kokoonpano", detail))

    lines.extend(_lineup_conflicts(stats))
    return lines


def _lineup_conflicts(stats: dict) -> list[str]:
    """Pelaajat, joilla havaittiin useampi nimi tai klaani samalla kartalla.

    Nolla on koko kokoonpanotaulun perusoletus: nimi on kartan ominaisuus eikä
    kierroksen, ja klaani seuraa pelaajaa eikä puolta. Taulu kirjoittaa moodin,
    joten rikkoutunut oletus näyttäisi siellä ehjältä -- tämä luku on ainoa
    paikka, jossa se näkyy. Nollaa ei tulosteta, koska se on odotusarvo.
    """
    lines: list[str] = []
    for key, label in (
        ("lineup_clan_conflicts", "Klaani vaihtui kesken"),
        ("lineup_name_conflicts", "Nimi vaihtui kesken"),
    ):
        count = int(stats.get(key, 0) or 0)
        if count:
            lines.append(
                _line(
                    label,
                    f"{count} pelaajalla oli useampi havainto samalla kartalla "
                    "-- tauluun kirjattiin useimmin havaittu",
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
            "kierroksen päättymisen jälkeen (havainto -- alue tulee "
            "pistepilvestä kuten muillakin)",
        ),
        (
            "grenade_ticks_without_players",
            "Tickillä ei rivejä",
            "heittoa ilman pelaajarivejä -- heittäjän aluetta ei voitu yrittää",
        ),
        (
            "grenades_sharing_an_entity_id",
            "Jaettu tunniste",
            "kranaattia jakaa pelin tunnisteen kierroksella (havainto)",
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
    pistepilven lähimmästä ruudusta johdettu arvio. Yhteen niputettuna
    raportin lukija luulisi molempia yhtä varmoiksi.

    Kolme lisäriviä ovat Story 2.9:n mittarit, ja ne kerrotaan **joka ajolla**
    eikä kerran kalibroinnissa:

    ``Räjähdysalue``
        Kattavuus ``n/m`` ja osuus. Se on ainoa luku, joka vastaa
        kysymykseen "kuinka moni utility-rivi jää ilman aluetta".
    ``Etäisyys ruutuun``
        Mediaani, p90 ja suurin. Asetus kertoo kynnyksen; tämä kertoo, mihin
        mittaus oikeasti osui -- ja suurin luku on se, joka osoittaa miksi
        kynnys on olemassa.
    ``Kynnyksen takana``
        Räjähdykset, joille lähin ruutu löytyi mutta jäi kynnyksen taakse.
        Se on kynnyksen **hinta**, ja se on eri asia kuin tyhjä pistepilvi:
        molemmissa alue puuttuu, mutta vain tässä etäisyys on tiedossa. Rivi
        näkyy vain kun luku on nollasta poikkeava.

    **Rivi ``Heittäjä ilman riviä`` on Story 2.10:n jäljiltä.** Heiton alue
    on heittäjän oma ``m_szLastPlaceName`` samalta tickiltä, joten ilman
    hänen riviään alue jää tyhjäksi eikä sitä voi korvata: pistepilvi nimeää
    räjähdyksiä, ei heittoja. Ennen pawnittoman rivin ohitusta tämä tapaus
    kaatoi ajon; nyt heitto valuisi ilman omaa riviään hiljaa ylläolevaan
    ``ilman aluetta`` -lukuun. Rivi näkyy vain kun luku on nollasta
    poikkeava, ja odotusarvo on nolla.

    **Rivi ``Nimetön alue`` poistui Story 2.9:ssä.** Se kertoi tapauksesta
    "lähin pelaaja löytyi, mutta pelillä ei ole nimeä hänen alueelleen", eikä
    sitä voi enää syntyä: pistepilveen ei pääse nimetöntä ruutua, joten
    kynnyksen sisällä osuva räjähdys saa aina nimen. Sen tilalla on
    ``Kynnyksen takana``, joka vastaa samaan kysymykseen -- "alue puuttui,
    mutta mittaus onnistui" -- oikealla syyllä. Merkintä on tässä siksi, että
    kahta ajoa version yli vertaava näkee rivin kadonneen ja saa tietää miksi.
    """
    observed = int(stats.get("utility_area_observed", 0) or 0)
    from_cloud = int(stats.get("utility_area_point_cloud", 0) or 0)
    beyond = int(stats.get("utility_area_beyond_threshold", 0) or 0)
    without_area = int(stats.get("utility_without_area", 0) or 0)
    lines = [
        _line(
            "Utilityn alue",
            f"{observed} havaittua, {from_cloud} pistepilvestä, "
            f"{without_area} ilman aluetta",
        )
    ]

    coverage = stats.get("utility_detonation_area_coverage")
    if coverage:
        # Nimittäjä ei voi olla nolla: vaihe jättää luvun kokonaan pois, jos
        # räjähdyksiä ei ollut. Sama sääntö kuin muillakin puuttuvilla
        # avaimilla -- "0/0" olisi väite, jota mikään ei tue.
        named, total = coverage
        lines.append(
            _line("Räjähdysalue", f"{named}/{total} nimetty ({named / total:.0%})")
        )
    spread = stats.get("utility_snap_distance")
    if spread:
        median, p90, largest = spread
        lines.append(
            _line(
                "Etäisyys ruutuun",
                f"mediaani {median:.0f}, p90 {p90:.0f}, suurin {largest:.0f} "
                "yksikköä",
            )
        )
    orphans = int(stats.get("grenade_throwers_without_row") or 0)
    if orphans:
        lines.append(
            _line(
                "Heittäjä ilman riviä",
                f"{orphans} heittoa jäi ilman aluetta (heittäjää ei ollut "
                "heiton tickin riveissä)",
            )
        )
    if beyond:
        lines.append(
            _line(
                "Kynnyksen takana",
                f"{beyond} räjähdystä (lähin ruutu löytyi, mutta se on "
                "kauempana kuin area_snap_units)",
            )
        )
    return lines


def _callouts(stats: dict) -> list[str]:
    """Pistepilven rivit ``parse``-tulosteeseen.

    **Alueiden määrä on tärkeämpi kuin ruutujen.** Ruutujen määrä kertoo vain
    ruudun koon; alueiden määrä kertoo, tunnistiko pilvi kartan. Mitattu
    2026-08-30: Ancient 18, Nuke 29, Anubis 28, Inferno 24. Yksinumeroinen
    luku tarkoittaisi, että ``last_place_name`` tulee enimmäkseen tyhjänä --
    ja silloin jokainen räjähdysalue olisi arvausta.

    Havainnoista kerrotaan **suhde eikä pelkkä summa**. Osoittaja on taulun
    oma ``callout_observations`` (kelvolliset rivit) ja nimittäjä
    diagnostiikan ``callout_cloud_rows_read`` (koko tickiluku); vain
    jälkimmäistä ei voi lukea valmiista taulusta, joten koko rivi näkyy vain
    tuoreesta ajosta. Kelvollisten osuus on mitatussa aineistossa 71-78 %, ja
    romahdus tarkoittaisi rikkinäistä suodatinta.
    """
    if "callouts_unreadable" in stats:
        return [
            _line("Pistepilvi", f"lukuja ei saatu ({stats['callouts_unreadable']})")
        ]
    if "callout_cells" not in stats:
        return []

    cells = int(stats.get("callout_cells", 0) or 0)
    areas = int(stats.get("callout_areas", 0) or 0)
    lines = [
        _line("Pistepilvi", f"{cells} ruutua, {areas} aluetta")
        if cells
        else _line("Pistepilvi", "tyhjä -- yhtäkään räjähdysaluetta ei nimetä")
    ]

    read = int(stats.get("callout_cloud_rows_read", 0) or 0)
    # Kelvolliset rivit tulevat taulusta eikä toisesta laskurista: ne ovat
    # ruutujen havaintojen summa, ja kaksi lähdettä samalle luvulle voisi
    # erkaantua.
    usable = int(stats.get("callout_observations", 0) or 0)
    if read:
        lines.append(
            _line(
                "Pilven havainnot",
                f"{usable}/{read} tickiriviä kelpasi ({usable / read:.0%} "
                "elossa ja alue tiedossa)",
            )
        )
    reason = stats.get("callout_cloud_empty_reason")
    if reason:
        lines.append(_line("Pilvi tyhjä koska", str(reason)))
    return lines


def _map_name(stats: dict) -> list[str]:
    """Kartan nimi ``parse``-tulosteeseen -- myös kun sitä ei saatu.

    Rivi on aina, ja se on koko pointti. Nimi on ``aggregate``n ainoa keino
    yhdistää kaksi demoa samaksi kartaksi, eikä FACEIT-tunnisteesta sitä voi
    päätellä. Jos demoparser2 nimeäisi ``map_name``-kentän uudelleen, jokainen
    demo palaisi omaksi karttahaaraksi -- ja ilman tätä riviä se tapahtuisi
    ilman yhtään merkkiä. Sama vikaluokka kuin Story 2.10:n pawnittomalla
    pelaajalla: hiljainen paluu huonompaan tulokseen.

    Nimi tulee **valmiista taulusta**, joten se näkyy myös ohitetusta ajosta.
    Syy puuttumiselle tulee diagnostiikasta ja näkyy vain tuoreesta ajosta,
    koska valmis taulu ei tiedä sitä.
    """
    if "match_unreadable" in stats:
        return [_line("Kartta", f"lukuja ei saatu ({stats['match_unreadable']})")]
    if "map_name" not in stats:
        return []

    name = stats.get("map_name")
    if name:
        return [_line("Kartta", f"{name} (havaittu demon otsikosta)")]

    lines = [
        _line(
            "Kartta",
            "otsikossa ei ollut kartan nimeä -- aggregate päättelee sen "
            "tunnisteesta",
        )
    ]
    reason = stats.get("header_map_name_missing_reason")
    if reason:
        lines.append(_line("Kartta puuttuu koska", str(reason)))
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
            economy=settings.economy,
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
    result.append("Rahaluvut ovat $/pelaaja ostoajan lopussa. Käytössä = jäljellä +")
    result.append("käytetty; jäljellä on saldo ostojen jälkeen, joten")
    result.append("säästökierroksella se on suuri.")
    return "\n".join(result)


@app.command("aggregate")
def aggregate(
    team: str | None = typer.Option(
        None,
        "--team",
        help=(
            "Joukkueen tunniste (classified/-hakemiston nimi) tai sen "
            "yksikäsitteinen alkuosa. Ilman tätä ajo päättyy virheeseen, "
            "joka listaa arkiston joukkueet."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--pakota",
        help="Aggregoi vaikka manifesti täsmäisi.",
    ),
) -> None:
    """Kokoa joukkueen luokitellut kierrokset yhdeksi report.json-tiedostoksi.

    Vaihe laskee kaiken: pelaajamäärät alueittain jokaisessa näytepisteessä,
    utilityn heitto- ja räjähdysalueet aikaikkunoineen sekä ensikontaktin
    alueet -- kartta, puoli ja kierrostyyppi kerrallaan, jokainen väite
    otannallaan. Se ei valitse mitä raportissa sanotaan; sen tekee render.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    result = aggregate_stage.run(
        settings.thresholds,
        settings.league,
        archive,
        team,
        aggregate_settings=settings.aggregate,
        force=force,
    )
    typer.echo(_render_aggregate(result))


def _render_aggregate(result: StageResult) -> str:
    """Kokoa ``aggregate``-komennon yhteenveto.

    Erotettu omaksi funktiokseen, jotta tuloste on testattavissa ilman
    komentorivin ajamista. Otannat ovat tulosteen tärkein osa: käyttäjä
    tarkistaa niistä, tuliko mukaan se aineisto, jonka hän odotti.
    """
    stats = result.stats
    lines: list[str] = []
    lines.append(f"{'Ohitettu' if result.skipped else 'Aggregoitu'}: {result.unit}")

    if result.reason:
        lines.append(_line("Syy", result.reason))

    # Nimi ennen kokoonpanoja: se on ensimmäinen asia, jonka käyttäjä
    # tarkistaa, ja lähde kertoo onko se havainto vai tunniste sen paikalla.
    if stats.get("display_name_source") == "clan_name":
        lines.append(_line("Nimi", f"{stats.get('display_name')} (havaittu demoista)"))
    elif "display_name_source" in stats:
        lines.append(
            _line(
                "Nimi",
                "ei havaittu -- raportti puhuu tunnisteesta "
                f"{stats.get('display_name')}",
            )
        )
    alternatives = stats.get("display_name_alternatives") or []
    if alternatives:
        lines.append(
            _line(
                "Muut havaitut nimet",
                f"{', '.join(str(n) for n in alternatives)} (demot antavat "
                "joukkueelle useamman nimen)",
            )
        )

    lineups = stats.get("lineup_keys") or []
    if len(lineups) > 1:
        lines.append(
            _line(
                "Kokoonpanot",
                f"{', '.join(str(k) for k in lineups)} (liitetty samaksi "
                "joukkueeksi yhteisten pelaajien perusteella)",
            )
        )
    roster = stats.get("roster") or []
    if roster:
        # Nimet tulosteeseen, tunnisteet raporttiin: komentorivin tuloste on
        # silmäys, ja kuusi SteamID64:ää täyttäisi sen kertomatta enempää.
        # Nimetön pelaaja sanotaan ääneen eikä pudoteta.
        named = ", ".join(
            str(entry.get("display_name") or entry.get("player_id"))
            for entry in roster
        )
        without = sum(1 for entry in roster if not entry.get("display_name"))
        lines.append(
            _line(
                "Rosteri",
                f"{len(roster)} pelaajaa havaittu: {named}"
                + (
                    f" ({without} ilman nimeä, tunniste sen paikalla)"
                    if without
                    else ""
                ),
            )
        )

    lines.append(
        _line(
            "Otanta",
            f"{int(stats.get('demos', 0) or 0)} demoa, "
            f"{int(stats.get('rounds', 0) or 0)} kierrosta",
        )
    )
    sample = stats.get("sample") or {}
    if sample:
        # Lokeroiden avaimet ovat englanniksi, koska ne ovat osa report.jsonin
        # sopimusta; tuloste on suomeksi kuten kaikki muukin käyttäjälle
        # näkyvä teksti. Sama työnjako kuin ROUND_TYPE_FI:llä.
        lines.append(
            _line(
                "Lokerot",
                ", ".join(
                    f"{SAMPLE_BUCKET_FI[name]} {sample[name]['demos']} demoa / "
                    f"{sample[name]['rounds']} kierrosta"
                    for name in SAMPLE_BUCKETS
                    if name in sample
                ),
            )
        )

    unclassified = int(stats.get("unclassified", 0) or 0)
    if unclassified:
        lines.append(
            _line(
                "Luokittelemattomat",
                f"{unclassified} kierrosta (havainto puuttuu, ei mukana "
                "rakenteessa)",
            )
        )

    unpaired = int(stats.get("unpaired_detonations", 0) or 0)
    if unpaired:
        lines.append(
            _line(
                "Parittomat räjähdykset",
                f"{unpaired} (heittoriviä ei löytynyt; ei mukana utilityn "
                "luvuissa)",
            )
        )

    # Poikkeamat omalle riville heti otannan jälkeen: ne ovat epicin
    # arvokkain tuotos, ja käyttäjä tarkistaa juuri niistä, näkyikö
    # kynnyksen säätö. Nolla poikkeamaa kirjoitetaan **kattavuuden kanssa**,
    # koska nolla on havainto vain siitä, mitä tutkittiin.
    scan = stats.get("anomaly_scan") or {}
    anomalies = stats.get("anomalies") or []
    if scan:
        rules = ", ".join(str(name) for name in scan.get("rules") or [])
        scanned = int(scan.get("rounds_scanned", 0) or 0)
        crunch_rounds = int(scan.get("crunch_rounds", 0) or 0)
        advance_rounds = int(scan.get("advance_rounds", 0) or 0)
        stack_rounds = int(scan.get("stack_rounds", 0) or 0)
        blind = scan.get("demos_without_orientation") or []
        no_groups = scan.get("demos_without_site_groups") or []
        deferred = scan.get("rules_deferred") or []
        detail = (
            f"{len(anomalies)} riviä; säännöt {rules} ajettiin "
            f"{scanned} kierrokselle -- crunch voi osua {crunch_rounds}, "
            f"eteneminen {advance_rounds} ja stack {stack_rounds}"
        )
        if deferred:
            detail += f"; ajamatta {', '.join(str(n) for n in deferred)}"
        if blind:
            detail += (
                f"; ilman alueorientaatiota {len(blind)} demoa: "
                f"{', '.join(str(d) for d in blind)}"
            )
        # Vaiennetut demot omana lukunaan eikä orientaatiottomien perässä: ne
        # ovat eri sokea piste (siteet eivät erotu tasoina) ja koskevat eri
        # sääntöä. Yhteen luetteloon niputettuina käyttäjä lukisi ne samaksi
        # puutteeksi ja etsisi korjausta väärästä paikasta.
        if no_groups:
            detail += (
                f"; ilman siteryhmiä {len(no_groups)} demoa: "
                f"{', '.join(str(d) for d in no_groups)}"
            )
        lines.append(_line("Poikkeamat", detail))
        for entry in anomalies:
            types = ", ".join(str(t) for t in entry.get("round_types") or [])
            # Stackin pelaajamäärä on murtoluku myös tulosteessa: neljä
            # viidestä on puolustuksen valinta, neljä neljästä on se mitä
            # jäljellä oli, eikä pelkkä luku erota niitä. Muilla säännöillä
            # nimittäjää ei ole, koska niitä ei mitattu.
            alive = entry.get("alive_at_max")
            players = (
                players_text(int(entry["players_max"]))
                if alive is None
                else f"{int(entry['players_max'])}/{int(alive)} pelaajaa"
            )
            lines.append(
                f"  {entry['rule']} {entry['map_name']} {entry['side']} "
                f"{types}: {entry['area']} {players} "
                f"({entry['n']}/{entry['m']})"
            )

    for entry in stats.get("maps") or []:
        lines.append("")
        # Ehto on **tuntemattomasta**, ei tunnetuista: lähteitä on kolme
        # (``demo_header``, ``map_demo_id``, ``unknown``), ja tunnettujen
        # luetteleminen tekisi jokaisesta uudesta lähteestä hiljaa
        # "tuntemattoman".
        source = (
            " (nimi tuntematon)" if entry["map_name_source"] == "unknown" else ""
        )
        lines.append(
            f"{entry['map_name']}{source}: {entry['demos']} demoa, "
            f"{entry['rounds']} kierrosta"
        )
        for side in entry["sides"]:
            types = ", ".join(
                f"{name} {count}" for name, count in side["round_types"].items()
            )
            small = side["small_samples"]
            note = f"  [pieni otanta: {', '.join(small)}]" if small else ""
            lines.append(f"  {side['side']}: {types}{note}")

    for missing in stats.get("missing_demos") or []:
        lines.append("")
        lines.append(_line("Puuttuva demo", f"{missing['match']}: {missing['reason']}"))

    lines.append("")
    for path in result.outputs:
        lines.append(_line("Tulos", str(path)))
    if result.manifest_path is not None:
        lines.append(_line("Manifesti", str(result.manifest_path)))
    lines.append(_line("Ajoaika", _seconds(result.duration_s)))
    return "\n".join(lines)


@app.command("report")
def report(
    team: str | None = typer.Option(
        None,
        "--team",
        help=(
            "Joukkueen tunniste (aggregates/-hakemiston nimi) tai sen "
            "yksikäsitteinen alkuosa. Ilman tätä ajo päättyy virheeseen, "
            "joka listaa aggregoidut joukkueet."
        ),
    ),
) -> None:
    """Kirjoita joukkueen report.jsonista luettava Markdown-raportti.

    Vaihe ei laske mitään: jokainen luku tulee aggregoinnista sellaisenaan.
    Raportti saa aikaleimatun nimen, joten uusi ajo ei koskaan ylikirjoita
    aiempaa -- eikä komennossa ole siksi --pakota-valintaa.

    Karsintasäännöt (``[report]``, Story 2.13) päättävät, mitkä rivit
    raporttiin kirjoitetaan. Ne eivät muuta report.jsonia: säännön
    kääntäminen pois ja komennon ajaminen uudelleen tuo rivin takaisin ilman
    aggregointia.
    """
    settings = load_settings()
    archive = archive_paths(settings.project)
    result = render_stage.run(settings.report, archive, team)
    typer.echo(_render_report(result))


def _render_report(result: StageResult) -> str:
    """Kokoa ``report``-komennon tuloste.

    Tärkein rivi on tuloksen polku: käyttäjä avaa tiedoston seuraavaksi.
    Luvut kertovat mitä siihen päätyi, jotta puuttuva kartta tai puuttuva demo
    huomataan ennen kuin raportti liitetään Discordiin.
    """
    stats = result.stats
    # Ensimmäinen rivi on tiedoston polku, koska se on ainoa asia, jota
    # käyttäjä tarvitsee seuraavaksi. Joukkuetunniste ei kelpaa: se on 16
    # merkin tiiviste, jonka käyttäjä juuri itse kirjoitti komentoriville.
    written = str(result.outputs[0]) if result.outputs else "(ei tiedostoa)"
    lines = [f"Raportti kirjoitettu: {written}"]

    team = str(stats.get("team_key", result.unit))
    if not stats.get("team_name_known", False):
        team += " (joukkueen nimi ei tiedossa)"
    lines.append(_line("Joukkue", team))

    lines.append(
        _line(
            "Otanta",
            f"{int(stats.get('demos', 0) or 0)} demoa, "
            f"{int(stats.get('rounds', 0) or 0)} kierrosta",
        )
    )
    maps = stats.get("maps") or []
    lines.append(
        _line(
            "Kartat",
            ", ".join(str(name) for name in maps) if maps else "ei yhtään karttaa",
        )
    )
    missing = int(stats.get("missing_demos", 0) or 0)
    if missing:
        lines.append(
            _line("Puuttuvat demot", f"{missing} kpl -- lueteltu raportissa")
        )
    unclassified = int(stats.get("unclassified", 0) or 0)
    if unclassified:
        lines.append(
            _line(
                "Luokittelemattomat",
                f"{unclassified} kierrosta -- mainittu raportin yhteenvedossa",
            )
        )
    lines.append(
        _line(
            "Laajuus",
            f"{int(stats.get('lines', 0) or 0)} riviä, "
            f"{int(stats.get('characters', 0) or 0)} merkkiä",
        )
    )

    lines.append("")
    if result.manifest_path is not None:
        lines.append(_line("Manifesti", str(result.manifest_path)))
    lines.append(_line("Ajoaika", _seconds(result.duration_s)))
    return "\n".join(lines)


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
