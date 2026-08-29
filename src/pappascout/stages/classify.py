"""``classify`` -- putken toinen vaihe: kierrostaulusta kierrostyypit.

Vaihe lukee ``parsed/<map_demo_id>/rounds.parquet``:n ja kirjoittaa
``classified/<team_key>/<map_demo_id>.parquet``-taulun, sen manifestin ja saman
sisällön luettavana kierroslistana ``<map_demo_id>.md``. **Demoa ei lueta eikä
``parsed/``-hakemistoon kirjoiteta** -- kaikki tämän vaiheen arvot ovat
johdettuja, ja ne lasketaan joka ajolla uudelleen puhtailla
``domain.economy``-funktioilla.

Molemmat joukkueet, yksi rivi
-----------------------------
Kierrostaulussa on kaksi riviä per kierros. Luokittelu tehdään **molemmille**
joukkueille samassa ajossa, mutta tulos on yksi rivi per kierros
subjektijoukkueen näkökulmasta: subjektin tyyppi on ``round_type`` ja
vastustajan ``opp_round_type``. Sama demo voidaan luokitella myös toiselle
joukkueelle -- silloin syntyy oma tulos omaan ``classified/<team_key>/``
-hakemistoon, eikä ``parse``-vaihetta ajeta uudelleen.

Yksi polku kierroslistalle
--------------------------
Kierroslistan rivit rakennetaan **aina** valmiista ``CLASSIFIED``-taulusta
funktiolla :func:`round_list_rows`, sekä tuoreessa että ohitetussa ajossa. Kaksi
polkua erkanisi ennemmin tai myöhemmin, ja silloin ``--show`` näyttäisi
ohituksen jälkeen eri luvut kuin ensimmäisellä ajolla.

``team_key`` tässä storyssa
---------------------------
Joukkueindeksi (``index/teams.json``) ja sen kanssa oikea ``team_key`` syntyvät
vasta ``select``-vaiheessa Epicissä 3. Siihen asti subjekti valitaan
``--team``-valinnalla suoraan **kokoonpanotunnisteella** (``lineup_key``), ja
sitä käytetään myös hakemistonimenä. Kun indeksi tulee, kokoonpanot liitetään
saman ``team_key``:n alle eikä tämän vaiheen logiikka muutu.

Samasta syystä ``is_league`` ja ``roster_class`` kirjoitetaan tyhjinä: ne
tulevat joukkueindeksistä ja ottelutiedoista, joita Epic 1 ei hae. Arvaus
("luultavasti liigaottelu") olisi tässä pahempi kuin tyhjä.

Uudelleenajo
------------
Manifestin ``params_hash`` lasketaan **vain** ``[thresholds]``- ja
``[league]``-osioista (AD-3), ja ``tool_versions`` on tyhjä, koska laskenta on
puhdasta domain-koodia. Kynnysarvon muuttaminen invalidoi siis tämän vaiheen
muttei parsintaa: tulos valmistuu sekunneissa, koska demoa ei lueta.

Syötteenä on ``parse``-vaiheen tulos. Sen tunniste kirjoitetaan
``ManifestInput.sha256``-kenttään, mutta **se ei ole tiedoston tiiviste** vaan
parsinnan manifestin sisällöstä laskettu parametrihash (ks.
:func:`_parse_fingerprint`). Kenttä on manifestimallissa nimetty tiivisteeksi,
koska ``parse`` kirjoittaa siihen demon sha256:n; tässä vaiheessa syöte on
toisen vaiheen tulos, jolla ei ole omaa tiivistettä, joten sen identiteetti
lasketaan manifestista. Vertailu toimii samoin kummassakin tapauksessa:
sama arvo tarkoittaa samaa syötettä.
"""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath

import polars as pl

from pappascout.archive.atomic_write import atomic_path, atomic_write_text
from pappascout.archive.manifest import Manifest, ManifestInput, compute_params_hash
from pappascout.archive.paths import (
    ArchivePaths,
    classified,
    classified_manifest,
    classified_round_list,
    parsed_manifest,
    parsed_table,
    safe_component,
)
from pappascout.constants import ROUND_TYPE_FI, UNCLASSIFIED
from pappascout.domain.economy import (
    Decision,
    classify_round,
    loss_counts,
    per_player,
)
from pappascout.domain.models import LeagueSettings, ThresholdSettings
from pappascout.domain.schemas import CLASSIFIED, ROUNDS, validate
from pappascout.errors import PappascoutError, SchemaError
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "TABLE",
    "TOOLS",
    "ROUND_LIST_COLUMNS",
    "run",
    "resolve_team",
    "team_keys",
    "classify_rounds",
    "round_list_rows",
    "round_list_cells",
    "render_round_list_markdown",
]

STAGE = "classify"
TABLE = "classified"

#: Tyhjä: luokittelu on puhdasta domain-laskentaa, eikä minkään ulkopuolisen
#: kirjaston versio muuta sen tulosta (manifest-moduulin sääntö).
TOOLS: tuple[str, ...] = ()


def run(
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    archive: ArchivePaths,
    map_demo_id: str,
    team: str | None,
    *,
    force: bool = False,
) -> StageResult:
    """Luokittele yhden demon kierrokset yhden joukkueen näkökulmasta.

    Args:
        thresholds: ``[thresholds]``-osio.
        league: ``[league]``-osio. Molemmat ovat mukana parametrihashissa
            (AD-3), eikä vaihe näe muita osioita.
        archive: Arkiston polut.
        map_demo_id: Yksikön tunniste.
        team: Subjektijoukkueen kokoonpanotunniste tai sen yksikäsitteinen
            alkuosa. ``None`` tuottaa suomenkielisen virheen, joka listaa demon
            kaksi kokoonpanoa.
        force: Ohita manifestin täsmäys ja luokittele joka tapauksessa.

    Returns:
        :class:`~pappascout.stages.StageResult`, jonka ``stats`` sisältää
        kierrosten määrän, tyyppijakauman ja koko kierroslistan riveinä.

    Raises:
        ~pappascout.errors.PappascoutError: Jos demoa ei ole parsittu tai
            ``team`` ei täsmää kumpaankaan kokoonpanoon.
        ~pappascout.errors.SchemaError: Jos kierrostaulu tai tulos ei vastaa
            sopimusta.
    """
    aloitus = time.perf_counter()
    map_demo_id = safe_component(map_demo_id, "map_demo_id")

    rounds, numeroimattomat = _read_rounds(archive, map_demo_id)
    parse_manifest = _read_parse_manifest(archive, map_demo_id)
    team_key = resolve_team(rounds, team, map_demo_id)

    table_rel = classified(team_key, map_demo_id)
    lista_rel = classified_round_list(team_key, map_demo_id)
    manifest_rel = classified_manifest(team_key, map_demo_id)
    table_abs = archive.resolve(table_rel)
    lista_abs = archive.resolve(lista_rel)
    manifest_abs = archive.resolve(manifest_rel)

    inputs = [
        ManifestInput(
            result_id=parse_manifest.result_id,
            sha256=_parse_fingerprint(parse_manifest),
        )
    ]
    params_hash = _params_hash(thresholds, league)

    olemassa = Manifest.read_if_exists(manifest_abs)
    valmis = None
    if (
        not force
        and olemassa is not None
        and olemassa.is_current(
            inputs=inputs,
            params_hash=params_hash,
            tool_versions={},
            root=archive.root,
        )
    ):
        valmis = _usable_result(table_abs)
    if valmis is not None:
        return StageResult(
            stage=STAGE,
            unit=map_demo_id,
            status="ok",
            skipped=True,
            outputs=tuple(PurePosixPath(o) for o in olemassa.outputs),
            manifest_path=manifest_rel,
            reason=(
                "Tulos on ajan tasalla: manifesti täsmää eikä kierroksia "
                "tarvitse luokitella uudelleen."
            ),
            duration_s=time.perf_counter() - aloitus,
            stats=_stats(
                round_list_rows(valmis), team_key, lista_rel, numeroimattomat
            ),
        )

    df, rivit = classify_rounds(rounds, team_key, thresholds, map_demo_id)

    with atomic_path(table_abs) as tmp:
        df.write_parquet(tmp)
    atomic_write_text(
        lista_abs,
        render_round_list_markdown(
            rivit,
            map_demo_id=map_demo_id,
            team_key=team_key,
            thresholds=thresholds,
            league=league,
        ),
    )

    Manifest.new(
        result_id=str(PurePosixPath("classified") / team_key / map_demo_id),
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions={},
        status="ok",
        outputs=(str(table_rel), str(lista_rel)),
    ).write(manifest_abs)

    return StageResult(
        stage=STAGE,
        unit=map_demo_id,
        status="ok",
        skipped=False,
        outputs=(table_rel, lista_rel),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - aloitus,
        stats=_stats(rivit, team_key, lista_rel, numeroimattomat),
    )


# -- Syötteet -------------------------------------------------------------------


def _read_rounds(
    archive: ArchivePaths, map_demo_id: str
) -> tuple[pl.DataFrame, int]:
    """Lue ja validoi parsittu kierrostaulu.

    Numeroimattomat kierrokset (``round_no`` tyhjä) pudotetaan ennen
    luokittelua: loss count on kierrosten järjestykseen sidottu laskuri, joka
    ei voi käsitellä numeroimatonta riviä. Määrä palautetaan, jotta ajo voi
    kertoa siitä eikä rivi katoa hiljaa.

    Returns:
        ``(taulu, pudotettujen numeroimattomien kierrosten määrä)``.

    Raises:
        PappascoutError: Jos taulua ei ole, sitä ei voi lukea, se on tyhjä tai
            se kuuluu toiselle demolle.
    """
    polku = archive.resolve(parsed_table(map_demo_id, "rounds"))
    if not polku.is_file():
        raise PappascoutError(
            f"Demoa {map_demo_id} ei ole vielä parsittu: tiedostoa {polku} ei "
            "ole.\n"
            f"Aja ensin: uv run pappascout parse {map_demo_id}"
        )
    try:
        df = pl.read_parquet(polku)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise PappascoutError(
            f"Kierrostaulua {polku} ei voitu lukea: {exc}\n"
            f"Aja parsinta uudelleen: uv run pappascout parse {map_demo_id} "
            "--pakota"
        ) from exc

    validate(df, ROUNDS, "rounds")

    # Väärä parquet oikeassa polussa luokiteltaisiin muuten väärän tunnisteen
    # alle, ja tulos näyttäisi täysin kelvolliselta.
    vieraat = sorted(
        {str(v) for v in df["map_demo_id"].unique().to_list() if v != map_demo_id}
    )
    if vieraat:
        raise PappascoutError(
            f"Kierrostaulu {polku} sisältää toisen demon rivejä "
            f"({', '.join(vieraat)}), vaikka sen pitäisi olla demon "
            f"{map_demo_id} taulu.\n"
            f"Poista hakemisto ja aja parsinta uudelleen: uv run pappascout "
            f"parse {map_demo_id} --pakota"
        )

    numeroidut = df.filter(pl.col("round_no").is_not_null())
    numeroimattomat = int(
        df.filter(pl.col("round_no").is_null())["round_raw"].n_unique()
    )
    if numeroidut.is_empty():
        raise PappascoutError(
            f"Kierrostaulussa {polku} ei ole yhtään numeroitua kierrosta, joten "
            "luokiteltavaa ei ole.\n"
            f"Aja parsinta uudelleen: uv run pappascout parse {map_demo_id} "
            "--pakota"
        )
    return numeroidut, numeroimattomat


def _read_parse_manifest(archive: ArchivePaths, map_demo_id: str) -> Manifest:
    """Lue ``parse``-manifesti; se on tämän vaiheen ainoa syöte.

    Raises:
        PappascoutError: Jos manifestia ei ole tai parsinta ei onnistunut.
            Vanhentuneen tai epäonnistuneen parsinnan päälle ei luokitella.
    """
    polku = archive.resolve(parsed_manifest(map_demo_id))
    manifest = Manifest.read_if_exists(polku)
    if manifest is None:
        raise PappascoutError(
            f"Parsinnan manifestia ei löytynyt polusta {polku}, joten "
            "luokittelun syötettä ei voi tunnistaa.\n"
            f"Aja ensin: uv run pappascout parse {map_demo_id}"
        )
    if manifest.status != "ok":
        raise PappascoutError(
            f"Demon {map_demo_id} parsinta on merkitty tilaan "
            f"{manifest.status!r}, joten sen tulosta ei luokitella.\n"
            f"Syy: {manifest.reason or 'ei kirjattu'}\n"
            f"Aja parsinta uudelleen: uv run pappascout parse {map_demo_id} "
            "--pakota"
        )
    return manifest


def _parse_fingerprint(manifest: Manifest) -> str:
    """Parsinnan tuloksen tunniste manifestin sisällöstä.

    **Ei tiedoston tiiviste.** Arvo kirjoitetaan ``ManifestInput.sha256``
    -kenttään, koska se on manifestimallin syötetunnistekenttä, mutta se on
    sha256 parsinnan manifestin *sisällöstä*: sen parametrihashista,
    syötteistä, työkaluversioista, tulostiedostoista ja tilasta. Kierrostaulua
    itseään ei hashata -- se on johdettu tuloste, ja sen identiteetti on juuri
    se, mistä se johdettiin.

    Luontihetki jätetään pois tarkoituksella: sama demo samoilla
    ``[parse]``-asetuksilla tuottaa saman tuloksen, eikä pelkkä uudelleenajo
    (``parse --pakota``) saa pakottaa uutta luokittelua. Kaikki muu on mukana,
    joten muuttunut demo, muuttunut asetus tai vaihtunut demoparser2 näkyy heti.
    """
    return compute_params_hash(
        {
            "params_hash": manifest.params_hash,
            "inputs": sorted([i.result_id, i.sha256] for i in manifest.inputs),
            "tool_versions": dict(manifest.tool_versions),
            "outputs": sorted(manifest.outputs),
            "status": manifest.status,
        }
    )


def _params_hash(thresholds: ThresholdSettings, league: LeagueSettings) -> str:
    """AD-3: vain nämä kaksi osiota vaikuttavat luokittelun tulokseen."""
    return compute_params_hash(
        {
            "thresholds": thresholds.model_dump(mode="json"),
            "league": league.model_dump(mode="json"),
        }
    )


def team_keys(archive: ArchivePaths, map_demo_id: str) -> list[str]:
    """Demon kokoonpanotunnisteet, jotta kaikki joukkueet voi luokitella.

    Luetaan kierrostaulusta, koska joukkueindeksiä ei vielä ole (Epic 3).
    """
    rounds, _ = _read_rounds(archive, safe_component(map_demo_id, "map_demo_id"))
    return [str(k["lineup_key"]) for k in _lineups(rounds)]


def resolve_team(df: pl.DataFrame, team: str | None, map_demo_id: str) -> str:
    """Tulkitse ``--team`` demon kokoonpanotunnisteeksi.

    Hyväksyy sekä täyden ``lineup_key``:n että sen yksikäsitteisen alkuosan --
    16 merkin tiiviste on epämukava kirjoittaa käsin.

    Raises:
        PappascoutError: Jos tunniste puuttuu, ei täsmää tai täsmää useampaan.
            Viesti listaa aina demon kokoonpanot, joten seuraava komento on
            suoraan kopioitavissa.
    """
    kokoonpanot = _lineups(df)
    if team is None:
        raise PappascoutError(
            "Kerro --team-valinnalla, kumman joukkueen näkökulmasta demo "
            f"{map_demo_id} luokitellaan.\n{_lineup_listing(kokoonpanot)}"
        )

    haku = team.strip().lower()
    osumat = [k for k in kokoonpanot if k["lineup_key"].lower() == haku]
    if not osumat:
        osumat = [k for k in kokoonpanot if k["lineup_key"].lower().startswith(haku)]
    if len(osumat) == 1:
        return safe_component(str(osumat[0]["lineup_key"]), "team_key")

    ongelma = (
        f"Kokoonpanotunniste {team!r} täsmää useampaan kuin yhteen kokoonpanoon."
        if osumat
        else (
            f"Kokoonpanotunniste {team!r} ei täsmää kumpaankaan demon "
            f"{map_demo_id} kokoonpanoon."
        )
    )
    raise PappascoutError(f"{ongelma}\n{_lineup_listing(kokoonpanot)}")


def _lineups(df: pl.DataFrame) -> list[dict[str, object]]:
    """Demon kokoonpanot tunnisteineen, aloituspuolineen ja voittoineen."""
    ensimmainen = df["round_no"].min()
    yhteenveto = (
        df.group_by("lineup_key")
        .agg(
            pl.col("won").fill_null(False).sum().alias("wins"),
            pl.col("side")
            .filter(pl.col("round_no") == ensimmainen)
            .first()
            .alias("first_side"),
        )
        .sort("lineup_key")
    )
    return [
        {
            "lineup_key": str(r["lineup_key"]),
            "wins": int(r["wins"] or 0),
            "first_side": None if r["first_side"] is None else str(r["first_side"]),
        }
        for r in yhteenveto.iter_rows(named=True)
    ]


def _lineup_listing(kokoonpanot: list[dict[str, object]]) -> str:
    if not kokoonpanot:
        return "Kierrostaulussa ei ole yhtään kokoonpanoa."
    rivit = [
        f"    {k['lineup_key']}  (aloitti puolella {k['first_side'] or '?'}, "
        f"voitti {k['wins']} kierrosta)"
        for k in kokoonpanot
    ]
    esimerkki = str(kokoonpanot[0]["lineup_key"])[:8]
    return (
        "Demon kokoonpanot ovat:\n"
        + "\n".join(rivit)
        + "\nAnna tunniste kokonaan tai sen alkuosa, esimerkiksi:\n"
        + f"    --team {esimerkki}"
    )


# -- Luokittelu ------------------------------------------------------------------


def classify_rounds(
    rounds: pl.DataFrame,
    team_key: str,
    thresholds: ThresholdSettings,
    map_demo_id: str,
) -> tuple[pl.DataFrame, list[dict[str, object]]]:
    """Rakenna ``CLASSIFIED``-taulu ja kierroslistan rivit kierrostaulusta.

    Julkinen, koska tämä on vaiheen koko päättely ilman tiedostoja: sen voi
    ajaa suoraan sekä käsin rakennetulla taululla että oikean demon
    kierrostaululla ilman arkistoa.

    Kumpikin joukkue luokitellaan omilla loss counteillaan ja omalla
    kierroshistoriallaan; subjektin rivi saa vastustajan tyypin
    ``opp_round_type``-sarakkeeseen.
    """
    subjekti = rounds.filter(pl.col("lineup_key") == team_key).sort("round_no")
    vastustaja = rounds.filter(pl.col("lineup_key") != team_key).sort("round_no")

    muut = int(vastustaja["lineup_key"].n_unique())
    if muut != 1:
        loydetyt = sorted({str(k) for k in rounds["lineup_key"].unique().to_list()})
        raise SchemaError(
            f"Kierrostaulussa on {len(loydetyt)} kokoonpanoa "
            f"({', '.join(loydetyt)}), joten vastustajaa ei voi tunnistaa "
            "yksikäsitteisesti. Kierrostaulussa on oltava tasan kaksi "
            "kokoonpanoa."
        )
    if subjekti["round_no"].to_list() != vastustaja["round_no"].to_list():
        raise SchemaError(
            "Joukkueiden kierrosnumerot eivät täsmää keskenään, joten "
            "vastustajan kierrostyyppiä ei voi liittää oikealle riville. "
            "Kierrostaulussa on oltava tasan kaksi riviä per kierros."
        )

    subjektin_paatokset, subjektin_loss = _classify_team(subjekti, thresholds)
    vastustajan_paatokset, _ = _classify_team(vastustaja, thresholds)

    taulu: list[dict[str, object]] = []
    for index, rivi in enumerate(subjekti.iter_rows(named=True)):
        paatos = subjektin_paatokset[index]
        taulu.append(
            {
                "map_demo_id": map_demo_id,
                "round_no": rivi["round_no"],
                "side": rivi["side"],
                "won": rivi["won"],
                "round_type": paatos.round_type,
                "opp_round_type": vastustajan_paatokset[index].round_type,
                "loss_count": subjektin_loss[index],
                "reason": paatos.reason,
                "inputs": paatos.inputs,
                # Tulevat joukkueindeksistä (Epic 3); arvaus olisi pahempi kuin
                # tyhjä.
                "is_league": None,
                "roster_class": None,
            }
        )

    df = pl.DataFrame(taulu, schema=dict(CLASSIFIED))
    validate(df, CLASSIFIED, TABLE)
    # Sama funktio kuin ohitetussa ajossa: kierroslistalla on vain yksi polku.
    return df, round_list_rows(df)


def _classify_team(
    team_rounds: pl.DataFrame, thresholds: ThresholdSettings
) -> tuple[list[Decision], list[int]]:
    """Luokittele yhden joukkueen kaikki kierrokset järjestyksessä.

    Palauttaa myös loss countit, jotta niitä ei lasketa kahdesti samalle
    joukkueelle -- kaksi laskentaa voisi erkaantua toisistaan.
    """
    laskurit = loss_counts(team_rounds, thresholds)
    rivit = team_rounds.to_dicts()
    paatokset = [
        classify_round(
            rivi,
            rivit[index - 1] if index > 0 else None,
            thresholds,
            loss_count=laskurit[index],
        )
        for index, rivi in enumerate(rivit)
    ]
    return paatokset, laskurit


# -- Kierroslista ------------------------------------------------------------------

#: Kierroslistan sarakkeet: ``(otsikko, avain)``. Sekä konsoli että Markdown
#: rakennetaan tästä, jotta ne eivät voi esittää eri sarakkeita.
ROUND_LIST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Kierros", "round_no"),
    ("Puoli", "side"),
    ("Tulos", "won"),
    ("Tyyppi", "round_type"),
    ("Vast.", "opp_round_type"),
    ("Käytössä", "money_available_per_player"),
    ("Jäljellä", "money_per_player"),
    ("Ostettu", "spent_per_player"),
    ("Varusteet", "equip_per_player"),
    ("Loss", "loss_count"),
    ("Perustelu", "reason"),
)

_TULOS_SANAT: dict[bool | None, str] = {True: "voitto", False: "häviö", None: "-"}


def round_list_rows(df: pl.DataFrame) -> list[dict[str, object]]:
    """Rakenna kierroslistan rivit valmiista ``CLASSIFIED``-taulusta.

    Ainoa polku kierroslistalle -- sekä tuore että ohitettu ajo kutsuu tätä.
    Per pelaaja -arvot lasketaan ``inputs``-rakenteesta samalla pyöristyksellä
    kuin perustelussa (``domain.economy.per_player``).
    """
    rivit: list[dict[str, object]] = []
    for r in df.sort("round_no").iter_rows(named=True):
        inputs = r["inputs"] or {}
        pelaajat = int(inputs.get("players") or 0)
        raha = inputs.get("money_freeze_end")
        kaytetty = inputs.get("money_spent")
        varusteet = inputs.get("equip_freeze_end")
        alku = inputs.get("equip_round_start")
        rivit.append(
            {
                "round_no": int(r["round_no"]),
                "side": str(r["side"]),
                "won": None if r["won"] is None else bool(r["won"]),
                "round_type": None if r["round_type"] is None else str(r["round_type"]),
                "opp_round_type": (
                    None if r["opp_round_type"] is None else str(r["opp_round_type"])
                ),
                "loss_count": r["loss_count"],
                "money_per_player": per_player(raha, pelaajat),
                "money_available_per_player": per_player(
                    None if raha is None and kaytetty is None
                    else int(raha or 0) + int(kaytetty or 0),
                    pelaajat,
                ),
                "spent_per_player": per_player(
                    None if varusteet is None or alku is None
                    else int(varusteet) - int(alku),
                    pelaajat,
                ),
                "equip_per_player": per_player(varusteet, pelaajat),
                "players": pelaajat or None,
                "reason": r["reason"],
            }
        )
    return rivit


def round_list_cells(rivi: dict[str, object]) -> tuple[str, ...]:
    """Yhden rivin solut :data:`ROUND_LIST_COLUMNS`-järjestyksessä."""
    solut: list[str] = []
    for _, avain in ROUND_LIST_COLUMNS:
        arvo = rivi.get(avain)
        if avain == "won":
            solut.append(_TULOS_SANAT[None if arvo is None else bool(arvo)])
        elif avain == "reason":
            solut.append(str(arvo or ""))
        elif avain in ("round_type", "opp_round_type"):
            solut.append(str(arvo) if arvo else UNCLASSIFIED)
        else:
            solut.append("-" if arvo is None else str(arvo))
    return tuple(solut)


def render_round_list_markdown(
    rivit: list[dict[str, object]],
    *,
    map_demo_id: str,
    team_key: str,
    thresholds: ThresholdSettings,
    league: LeagueSettings,
) -> str:
    """Kirjoita kierroslista Markdowniksi, jotta sen voi lukea demon rinnalla.

    Otsikkoon tulevat käytetyt kynnysarvot: ilman niitä lista ei kerro, mitä
    vasten päätökset tehtiin, eikä kalibrointikierros olisi jäljitettävissä.

    Tuloste on **toistettava**: samoista syötteistä syntyy tavu tavulta sama
    teksti. Ajohetki ei ole tässä vaan manifestin ``created_at``-kentässä --
    muuten tiedosto muuttuisi joka ajolla eikä eroa voisi katsoa.
    """
    osat: list[str] = []
    osat.append(f"# Kierroslista -- {map_demo_id}")
    osat.append("")
    osat.append(f"- Joukkue (kokoonpanotunniste): `{team_key}`")
    osat.append(f"- Kierroksia: {len(rivit)}")
    osat.append(
        f"- Liigaformaatti: MR{league.mr}, säännönmukaisia kierroksia "
        f"{thresholds.regulation_rounds}, pistoolikierrokset "
        f"{', '.join(str(r) for r in thresholds.pistol_rounds)}, jatkoajan "
        f"aloitusraha {league.ot_start_money} $"
    )
    osat.append(
        f"- Kynnykset ($/pelaaja): täysi osto vähintään "
        f"{thresholds.full_equip_min}, matala varustearvo voiton jälkeen "
        f"enintään {thresholds.anomaly_equip_max_after_win}; hävityn jälkeen "
        f"osto vaatii ostettua vähintään {thresholds.force_buy_min} ja erottuu "
        f"forceksi, jos taskuun jäi enintään "
        f"{thresholds.force_money_left_max} (muuten puoliosto), ja muuten eco"
    )
    osat.append(
        f"- Loss count: puoliajan alku {thresholds.loss_count_half_start}, rajat "
        f"{thresholds.loss_count_min}-{thresholds.loss_count_max}"
    )
    osat.append("")
    osat.append(
        "Kaikki rahaluvut ovat dollareita per pelaaja freezetimen lopussa. "
        "**Käytössä** = jäljellä + käytetty eli se raha, joka joukkueella oli "
        "ostoaikana. **Jäljellä** on saldo ostojen jälkeen, joten "
        "säästökierroksella se on suuri. **Ostettu** on varustearvon kasvu "
        "kierroksen alusta freezetimen loppuun."
    )
    osat.append("")

    otsikot = [o for o, _ in ROUND_LIST_COLUMNS]
    osat.append("| " + " | ".join(otsikot) + " |")
    osat.append("|" + "|".join(["---"] * len(otsikot)) + "|")
    for rivi in rivit:
        osat.append("| " + " | ".join(_md(s) for s in round_list_cells(rivi)) + " |")

    osat.append("")
    osat.append("## Kierrostyypit")
    osat.append("")
    for arvo, suomeksi in ROUND_TYPE_FI.items():
        osat.append(f"- `{arvo}` -- {suomeksi}")
    osat.append(f"- `{UNCLASSIFIED}` -- havainto puuttui, kierrosta ei luokiteltu")
    osat.append("")
    osat.append(
        "`is_league` ja `roster_class` jäävät tässä vaiheessa tyhjiksi: ne "
        "tulevat joukkueindeksistä, joka syntyy vasta Epicissä 3."
    )
    osat.append("")
    return "\n".join(osat)


def _md(teksti: str) -> str:
    """Suojaa solun sisältö Markdown-taulukkoa varten.

    Putkimerkki katkaisisi solun ja rivinvaihto koko taulukon; backtick
    aloittaisi koodijakson, joka söisi loput rivistä. Kaikki kolme tulevat
    perusteluista, jotka ovat vapaata tekstiä.
    """
    return (
        teksti.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "\\`")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


# -- Luvut ------------------------------------------------------------------------


def _stats(
    rivit: list[dict[str, object]],
    team_key: str,
    lista_rel: PurePosixPath,
    numeroimattomat: int,
) -> dict[str, object]:
    """Käyttäjälle näytettävät luvut.

    ``by_type`` sisältää vain oikeat kierrostyypit; luokittelemattomat ovat
    omana lukunaan, jotta niitä ei näytetä kahdesti.
    """
    jakauma: dict[str, int] = {}
    luokittelematta = 0
    for rivi in rivit:
        tyyppi = rivi["round_type"]
        if tyyppi is None:
            luokittelematta += 1
            continue
        avain = str(tyyppi)
        jakauma[avain] = jakauma.get(avain, 0) + 1
    return {
        "team_key": team_key,
        "rounds": len(rivit),
        "by_type": jakauma,
        "unclassified": luokittelematta,
        "unnumbered": numeroimattomat,
        "round_list": str(lista_rel),
        "rows": rivit,
    }


def _usable_result(table_abs: Path) -> pl.DataFrame | None:
    """Valmis tulos, jos se on luettavissa **ja** vastaa yhä sopimusta.

    Täsmäävä manifesti ei yksin riitä. Tulostaulun skeema voi muuttua ilman
    että manifestin sisältö muuttuu -- esimerkiksi kun ``CLASSIFIED``-sopimus
    saa uuden kentän -- ja silloin vanha tulos näyttäisi ajantasaiselta mutta
    puuttuisi uudet arvot. Luokittelu on halpaa (demoa ei lueta), joten
    epäkelpo tulos lasketaan mieluummin uudelleen kuin raportoidaan
    vajaana.

    Returns:
        Taulu, tai ``None`` jos se on lukukelvoton tai sopimuksen vastainen --
        kummassakin tapauksessa vaihe ajetaan uudelleen.
    """
    try:
        df = pl.read_parquet(table_abs)
    except (OSError, pl.exceptions.PolarsError):
        return None
    try:
        return validate(df, CLASSIFIED, TABLE)
    except SchemaError:
        return None
