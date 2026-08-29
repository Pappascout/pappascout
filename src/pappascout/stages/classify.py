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
Manifestin ``params_hash`` lasketaan **vain** ``[thresholds]``-, ``[league]``-
ja ``[economy]``-osioista (AD-3), ja ``tool_versions`` on tyhjä, koska laskenta
on puhdasta domain-koodia. Kynnysarvon muuttaminen invalidoi siis tämän
vaiheen muttei parsintaa: tulos valmistuu sekunneissa, koska demoa ei lueta.

``[economy]`` tuli mukaan Story 1.10:ssä. Puolioston ehto B kysyy, pystyykö
pelaaja normaaliin ostoon seuraavalla kierroksella, ja vastaus riippuu
häviöbonuksesta (``loss_bonus_steps``). Ilman osiota hashissa portaan
muuttaminen jättäisi vanhan tuloksen paikalleen ja näyttäisi ajan tasalla
olevalta.

Syötteenä on ``parse``-vaiheen tulos. Sen tunniste kirjoitetaan
``ManifestInput.sha256``-kenttään, mutta **se ei ole tiedoston tiiviste** vaan
parsinnan manifestin sisällöstä laskettu parametrihash (ks.
:meth:`~pappascout.archive.manifest.Manifest.fingerprint`). Kenttä on
manifestimallissa nimetty tiivisteeksi,
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
    CLASSIFY_COLUMNS,
    Decision,
    classify_round,
    loss_counts,
    per_player,
)
from pappascout.domain.models import (
    EconomySettings,
    LeagueSettings,
    ThresholdSettings,
)
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
    economy: EconomySettings,
    force: bool = False,
) -> StageResult:
    """Luokittele yhden demon kierrokset yhden joukkueen näkökulmasta.

    Args:
        thresholds: ``[thresholds]``-osio.
        league: ``[league]``-osio.
        economy: ``[economy]``-osio, **avainsanaparametrina**. Siitä
            luetaan ``loss_bonus_steps`` ja ``max_money`` (puolioston ehto
            B). Kaikki kolme osiota ovat mukana parametrihashissa (AD-3),
            eikä vaihe näe muita. Avainsana siksi, että kolme
            pydantic-osiota peräkkäin menisi positionaalisesti vaihtaen
            läpi ilman että mikään huomauttaisi.
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
    started = time.perf_counter()
    map_demo_id = safe_component(map_demo_id, "map_demo_id")

    rounds, unnumbered = _read_rounds(archive, map_demo_id)
    parse_manifest = _read_parse_manifest(archive, map_demo_id)
    team_key = resolve_team(rounds, team, map_demo_id)

    table_rel = classified(team_key, map_demo_id)
    list_rel = classified_round_list(team_key, map_demo_id)
    manifest_rel = classified_manifest(team_key, map_demo_id)
    table_abs = archive.resolve(table_rel)
    list_abs = archive.resolve(list_rel)
    manifest_abs = archive.resolve(manifest_rel)

    inputs = [
        # Syötteen tunniste on parsinnan MANIFESTIN sisällöstä, ei
        # kierrostaulun tiivisteestä: taulu on johdettu tuloste, ja sen
        # identiteetti on juuri se, mistä se johdettiin. Sama määritelmä
        # kuin aggregate-vaiheessa.
        ManifestInput(
            result_id=parse_manifest.result_id,
            sha256=parse_manifest.fingerprint(),
        )
    ]
    params_hash = _params_hash(thresholds, league, economy)

    existing = Manifest.read_if_exists(manifest_abs)
    ready = None
    if (
        not force
        and existing is not None
        and existing.is_current(
            inputs=inputs,
            params_hash=params_hash,
            tool_versions={},
            root=archive.root,
        )
    ):
        ready = _usable_result(table_abs)
    if ready is not None:
        return StageResult(
            stage=STAGE,
            unit=map_demo_id,
            status="ok",
            skipped=True,
            outputs=tuple(PurePosixPath(o) for o in existing.outputs),
            manifest_path=manifest_rel,
            reason=(
                "Tulos on ajan tasalla: manifesti täsmää eikä kierroksia "
                "tarvitse luokitella uudelleen."
            ),
            duration_s=time.perf_counter() - started,
            stats=_stats(
                round_list_rows(ready), team_key, list_rel, unnumbered
            ),
        )

    df, rows = classify_rounds(
        rounds, team_key, thresholds, map_demo_id, economy=economy
    )

    with atomic_path(table_abs) as tmp:
        df.write_parquet(tmp)
    atomic_write_text(
        list_abs,
        render_round_list_markdown(
            rows,
            map_demo_id=map_demo_id,
            team_key=team_key,
            thresholds=thresholds,
            league=league,
            economy=economy,
        ),
    )

    Manifest.new(
        result_id=str(PurePosixPath("classified") / team_key / map_demo_id),
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions={},
        status="ok",
        outputs=(str(table_rel), str(list_rel)),
    ).write(manifest_abs)

    return StageResult(
        stage=STAGE,
        unit=map_demo_id,
        status="ok",
        skipped=False,
        outputs=(table_rel, list_rel),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - started,
        stats=_stats(rows, team_key, list_rel, unnumbered),
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
    path = archive.resolve(parsed_table(map_demo_id, "rounds"))
    if not path.is_file():
        raise PappascoutError(
            f"Demoa {map_demo_id} ei ole vielä parsittu: tiedostoa {path} ei "
            "ole.\n"
            f"Aja ensin: uv run pappascout parse {map_demo_id}"
        )
    try:
        df = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise PappascoutError(
            f"Kierrostaulua {path} ei voitu lukea: {exc}\n"
            f"Aja parsinta uudelleen: uv run pappascout parse {map_demo_id} "
            "--pakota"
        ) from exc

    # validate puhuu oletuksena kehittäjälle ("lisää sarake tai korjaa taulun
    # tuottanut vaihe -- sopimus on tiedostossa domain/schemas.py"). Se on
    # väärä neuvo tässä: taulu tulee arkistosta, sen on kirjoittanut ohjelman
    # oma aiempi versio, eikä käyttäjä korjaa sitä koodia muokkaamalla.
    # Korjaus on ajaa parsinta uudelleen, joten se on myös se, mitä viesti
    # sanoo. Sarakkeen nimi säilyy diagnoosiksi.
    validate(
        df,
        ROUNDS,
        "rounds",
        advice=(
            "Taulu on parsittu ohjelman vanhemmalla versiolla. Aja parsinta "
            f"uudelleen: uv run pappascout parse {map_demo_id} --pakota"
        ),
    )

    # Väärä parquet oikeassa polussa luokiteltaisiin muuten väärän tunnisteen
    # alle, ja tulos näyttäisi täysin kelvolliselta.
    foreign = sorted(
        {str(v) for v in df["map_demo_id"].unique().to_list() if v != map_demo_id}
    )
    if foreign:
        raise PappascoutError(
            f"Kierrostaulu {path} sisältää toisen demon rivejä "
            f"({', '.join(foreign)}), vaikka sen pitäisi olla demon "
            f"{map_demo_id} taulu.\n"
            f"Poista hakemisto ja aja parsinta uudelleen: uv run pappascout "
            f"parse {map_demo_id} --pakota"
        )

    numbered = df.filter(pl.col("round_no").is_not_null())
    unnumbered = int(
        df.filter(pl.col("round_no").is_null())["round_raw"].n_unique()
    )
    if numbered.is_empty():
        raise PappascoutError(
            f"Kierrostaulussa {path} ei ole yhtään numeroitua kierrosta, joten "
            "luokiteltavaa ei ole.\n"
            f"Aja parsinta uudelleen: uv run pappascout parse {map_demo_id} "
            "--pakota"
        )
    return numbered, unnumbered


def _read_parse_manifest(archive: ArchivePaths, map_demo_id: str) -> Manifest:
    """Lue ``parse``-manifesti; se on tämän vaiheen ainoa syöte.

    Raises:
        PappascoutError: Jos manifestia ei ole tai parsinta ei onnistunut.
            Vanhentuneen tai epäonnistuneen parsinnan päälle ei luokitella.
    """
    path = archive.resolve(parsed_manifest(map_demo_id))
    manifest = Manifest.read_if_exists(path)
    if manifest is None:
        raise PappascoutError(
            f"Parsinnan manifestia ei löytynyt polusta {path}, joten "
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


def _params_hash(
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    economy: EconomySettings,
) -> str:
    """AD-3: vain nämä kolme osiota vaikuttavat luokittelun tulokseen.

    ``[economy]`` on mukana kokonaisena, vaikka luokittelu lukee siitä vain
    ``loss_bonus_steps``. Osittainen hash vaatisi listan siitä, mitä osion
    kentistä säännöt sattuvat lukemaan -- ja se lista vanhenisi hiljaa
    ensimmäisenä päivänä, jona sääntö lukee yhden kentän lisää. Hinta on
    tarpeeton uudelleenajo, kun jokin muu talousarvo muuttuu; se maksaa
    sekunteja, koska demoa ei lueta.
    """
    return compute_params_hash(
        {
            "thresholds": thresholds.model_dump(mode="json"),
            "league": league.model_dump(mode="json"),
            "economy": economy.model_dump(mode="json"),
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
    lineups = _lineups(df)
    if team is None:
        raise PappascoutError(
            "Kerro --team-valinnalla, kumman joukkueen näkökulmasta demo "
            f"{map_demo_id} luokitellaan.\n{_lineup_listing(lineups)}"
        )

    query = team.strip().lower()
    matches = [k for k in lineups if k["lineup_key"].lower() == query]
    if not matches:
        matches = [k for k in lineups if k["lineup_key"].lower().startswith(query)]
    if len(matches) == 1:
        return safe_component(str(matches[0]["lineup_key"]), "team_key")

    problem = (
        f"Kokoonpanotunniste {team!r} täsmää useampaan kuin yhteen kokoonpanoon."
        if matches
        else (
            f"Kokoonpanotunniste {team!r} ei täsmää kumpaankaan demon "
            f"{map_demo_id} kokoonpanoon."
        )
    )
    raise PappascoutError(f"{problem}\n{_lineup_listing(lineups)}")


def _lineups(df: pl.DataFrame) -> list[dict[str, object]]:
    """Demon kokoonpanot tunnisteineen, aloituspuolineen ja voittoineen."""
    first_round = df["round_no"].min()
    summary = (
        df.group_by("lineup_key")
        .agg(
            pl.col("won").fill_null(False).sum().alias("wins"),
            pl.col("side")
            .filter(pl.col("round_no") == first_round)
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
        for r in summary.iter_rows(named=True)
    ]


def _lineup_listing(lineups: list[dict[str, object]]) -> str:
    if not lineups:
        return "Kierrostaulussa ei ole yhtään kokoonpanoa."
    rows = [
        f"    {k['lineup_key']}  (aloitti puolella {k['first_side'] or '?'}, "
        f"voitti {k['wins']} kierrosta)"
        for k in lineups
    ]
    example = str(lineups[0]["lineup_key"])[:8]
    return (
        "Demon kokoonpanot ovat:\n"
        + "\n".join(rows)
        + "\nAnna tunniste kokonaan tai sen alkuosa, esimerkiksi:\n"
        + f"    --team {example}"
    )


# -- Luokittelu ------------------------------------------------------------------


def classify_rounds(
    rounds: pl.DataFrame,
    team_key: str,
    thresholds: ThresholdSettings,
    map_demo_id: str,
    *,
    economy: EconomySettings,
) -> tuple[pl.DataFrame, list[dict[str, object]]]:
    """Rakenna ``CLASSIFIED``-taulu ja kierroslistan rivit kierrostaulusta.

    Julkinen, koska tämä on vaiheen koko päättely ilman tiedostoja: sen voi
    ajaa suoraan sekä käsin rakennetulla taululla että oikean demon
    kierrostaululla ilman arkistoa.

    Kumpikin joukkue luokitellaan omilla loss counteillaan ja omalla
    kierroshistoriallaan; subjektin rivi saa vastustajan tyypin
    ``opp_round_type``-sarakkeeseen.
    """
    subject = rounds.filter(pl.col("lineup_key") == team_key).sort("round_no")
    opponent = rounds.filter(pl.col("lineup_key") != team_key).sort("round_no")

    others = int(opponent["lineup_key"].n_unique())
    if others != 1:
        found = sorted({str(k) for k in rounds["lineup_key"].unique().to_list()})
        raise SchemaError(
            f"Kierrostaulussa on {len(found)} kokoonpanoa "
            f"({', '.join(found)}), joten vastustajaa ei voi tunnistaa "
            "yksikäsitteisesti. Kierrostaulussa on oltava tasan kaksi "
            "kokoonpanoa."
        )
    if subject["round_no"].to_list() != opponent["round_no"].to_list():
        raise SchemaError(
            "Joukkueiden kierrosnumerot eivät täsmää keskenään, joten "
            "vastustajan kierrostyyppiä ei voi liittää oikealle riville. "
            "Kierrostaulussa on oltava tasan kaksi riviä per kierros."
        )

    subject_decisions, subject_loss = _classify_team(
        subject, thresholds, economy=economy
    )
    opponent_decisions, _ = _classify_team(opponent, thresholds, economy=economy)

    table: list[dict[str, object]] = []
    for index, row in enumerate(subject.iter_rows(named=True)):
        decision = subject_decisions[index]
        table.append(
            {
                "map_demo_id": map_demo_id,
                "round_no": row["round_no"],
                "side": row["side"],
                "won": row["won"],
                "round_type": decision.round_type,
                "opp_round_type": opponent_decisions[index].round_type,
                "loss_count": subject_loss[index],
                "reason": decision.reason,
                "inputs": decision.inputs,
                # Tulevat joukkueindeksistä (Epic 3); arvaus olisi pahempi kuin
                # tyhjä.
                "is_league": None,
                "roster_class": None,
            }
        )

    df = pl.DataFrame(table, schema=dict(CLASSIFIED))
    validate(df, CLASSIFIED, TABLE)
    # Sama funktio kuin ohitetussa ajossa: kierroslistalla on vain yksi polku.
    return df, round_list_rows(df)


def _classify_team(
    team_rounds: pl.DataFrame,
    thresholds: ThresholdSettings,
    *,
    economy: EconomySettings,
) -> tuple[list[Decision], list[int]]:
    """Luokittele yhden joukkueen kaikki kierrokset järjestyksessä.

    Palauttaa myös loss countit, jotta niitä ei lasketa kahdesti samalle
    joukkueelle -- kaksi laskentaa voisi erkaantua toisistaan.

    Riveiltä poimitaan **tasan** ``domain.economy.CLASSIFY_COLUMNS``, ja se on
    tarkoituksellista: sopimus siitä, mitä luokittelu lukee, on silloin
    koodissa eikä kommentissa. Sarakkeen pudottaminen listalta pudottaa sen
    myös päätöksestä, joten lista ei voi vanhentua hiljaa.
    """
    counters = loss_counts(team_rounds, thresholds)
    rows = team_rounds.select(list(CLASSIFY_COLUMNS)).to_dicts()
    decisions = [
        classify_round(
            row,
            rows[index - 1] if index > 0 else None,
            thresholds,
            economy=economy,
            loss_count=counters[index],
        )
        for index, row in enumerate(rows)
    ]
    return decisions, counters


# -- Kierroslista ------------------------------------------------------------------

#: Kierroslistan sarakkeet: ``(otsikko, avain)``. Sekä konsoli että Markdown
#: rakennetaan tästä, jotta ne eivät voi esittää eri sarakkeita.
#:
#: **Ratkaisevat luvut ovat taulukossa, eivät vain proosassa.** Hyvitys ja
#: kaksi pelaajalaskuria (``Aseist.``, ``Ostokyky``) ovat ne, joista
#: häviön jälkeinen luokka ratkeaa; ilman niitä lukija näkisi taulukossa
#: vain ``Jäljellä``-sarakkeen, joka on **joukkueen keskiarvo** eikä
#: ratkaise mitään. Keskiarvo on silti mukana, koska se kertoo joukkueen
#: kokonaistilanteen -- otsikko sanoo kumpi on kumpi.
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
    ("Bonus", "loss_bonus_if_lost"),
    ("Aseist.", "armed_of_players"),
    ("Ostokyky", "can_buy_of_players"),
    ("Perustelu", "reason"),
)

_RESULT_WORDS: dict[bool | None, str] = {True: "voitto", False: "häviö", None: "-"}


def _counter(value: object, players: int) -> str | None:
    """Pelaajalaskuri muodossa ``"4/5"``, tai ``None`` jos lukua ei ole.

    Nimittäjä on sama jakaja kuin per pelaaja -arvoissa, joten rivin kaikki
    luvut puhuvat samasta joukosta.
    """
    if value is None or not players:
        return None
    return f"{int(value)}/{players}"


def round_list_rows(df: pl.DataFrame) -> list[dict[str, object]]:
    """Rakenna kierroslistan rivit valmiista ``CLASSIFIED``-taulusta.

    Ainoa polku kierroslistalle -- sekä tuore että ohitettu ajo kutsuu tätä.
    Per pelaaja -arvot lasketaan ``inputs``-rakenteesta samalla pyöristyksellä
    kuin perustelussa (``domain.economy.per_player``).
    """
    rows: list[dict[str, object]] = []
    for r in df.sort("round_no").iter_rows(named=True):
        inputs = r["inputs"] or {}
        players = int(inputs.get("players") or 0)
        money = inputs.get("money_buy_end")
        spent = inputs.get("money_spent")
        equip = inputs.get("equip_buy_end")
        equip_start = inputs.get("equip_round_start")
        rows.append(
            {
                "round_no": int(r["round_no"]),
                "side": str(r["side"]),
                "won": None if r["won"] is None else bool(r["won"]),
                "round_type": None if r["round_type"] is None else str(r["round_type"]),
                "opp_round_type": (
                    None if r["opp_round_type"] is None else str(r["opp_round_type"])
                ),
                "loss_count": r["loss_count"],
                "money_per_player": per_player(money, players),
                "money_available_per_player": per_player(
                    None if money is None and spent is None
                    else int(money or 0) + int(spent or 0),
                    players,
                ),
                "spent_per_player": per_player(
                    None if equip is None or equip_start is None
                    else int(equip) - int(equip_start),
                    players,
                ),
                "equip_per_player": per_player(equip, players),
                "players": players or None,
                # Pelaajalaskurit näytetään muodossa "4/5": pelkkä luku
                # 4 ei kerro, oliko joukkue täysilukuinen -- ja juuri se
                # ratkaisee, mitä vasten kynnystä verrattiin.
                "loss_bonus_if_lost": inputs.get("loss_bonus_if_lost"),
                "armed_of_players": _counter(
                    inputs.get("players_armed"), players
                ),
                "can_buy_of_players": _counter(
                    inputs.get("players_can_buy"), players
                ),
                "reason": r["reason"],
            }
        )
    return rows


def round_list_cells(row: dict[str, object]) -> tuple[str, ...]:
    """Yhden rivin solut :data:`ROUND_LIST_COLUMNS`-järjestyksessä."""
    cells: list[str] = []
    for _, key in ROUND_LIST_COLUMNS:
        value = row.get(key)
        if key == "won":
            cells.append(_RESULT_WORDS[None if value is None else bool(value)])
        elif key == "reason":
            cells.append(str(value or ""))
        elif key in ("round_type", "opp_round_type"):
            cells.append(str(value) if value else UNCLASSIFIED)
        else:
            cells.append("-" if value is None else str(value))
    return tuple(cells)


def render_round_list_markdown(
    rows: list[dict[str, object]],
    *,
    map_demo_id: str,
    team_key: str,
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    economy: EconomySettings,
) -> str:
    """Kirjoita kierroslista Markdowniksi, jotta sen voi lukea demon rinnalla.

    Otsikkoon tulevat käytetyt kynnysarvot: ilman niitä lista ei kerro, mitä
    vasten päätökset tehtiin, eikä kalibrointikierros olisi jäljitettävissä.

    Tuloste on **toistettava**: samoista syötteistä syntyy tavu tavulta sama
    teksti. Ajohetki ei ole tässä vaan manifestin ``created_at``-kentässä --
    muuten tiedosto muuttuisi joka ajolla eikä eroa voisi katsoa.
    """
    parts: list[str] = []
    parts.append(f"# Kierroslista -- {map_demo_id}")
    parts.append("")
    parts.append(f"- Joukkue (kokoonpanotunniste): `{team_key}`")
    parts.append(f"- Kierroksia: {len(rows)}")
    parts.append(
        f"- Liigaformaatti: MR{league.mr}, säännönmukaisia kierroksia "
        f"{thresholds.regulation_rounds}, pistoolikierrokset "
        f"{', '.join(str(r) for r in thresholds.pistol_rounds)}, jatkoajan "
        f"aloitusraha {league.ot_start_money} $"
    )
    parts.append(
        f"- Kynnykset ($/pelaaja): täysi osto vähintään "
        f"{thresholds.full_equip_min}, matala varustearvo voiton jälkeen "
        f"enintään {thresholds.anomaly_equip_max_after_win}; hävityn jälkeen "
        f"osto vaatii ostettua vähintään {thresholds.force_buy_min}, muuten eco"
    )
    parts.append(
        f"- Puolioston kaksi ehtoa, **molempien** on täytyttävä: A) "
        f"vähintään {thresholds.armed_players_min} pelaajaa aseistettuna "
        f"(erottaa ecosta) ja B) vähintään "
        f"{thresholds.normal_buy_players_min} pelaajaa, joiden **oma** "
        f"saldo + häviöbonus on vähintään "
        f"{thresholds.normal_buy_money_min} $ (erottaa forcesta)"
    )
    parts.append(
        "- Ehto B lasketaan **pelaajakohtaisesta rahajakaumasta**, ei "
        "keskiarvosta: keskiarvo peittää jakauman ja voi osua arvoon, jota "
        "kukaan ei voi pitää. Häviöbonus on loss countin porras (portaat "
        + ", ".join(str(s) for s in economy.loss_bonus_steps)
        + f" $), ja summa katkaistaan rahakattoon {economy.max_money} $. "
        "Puoliajan viimeisellä kierroksella ehtoa B ei lasketa lainkaan: "
        "raha ei siirry pistoolikierrokselle eikä jatkoajalle, joten sitä "
        "ei ole jätetty varaa varten."
    )
    parts.append(
        f"- Loss count: puoliajan alku {thresholds.loss_count_half_start}, rajat "
        f"{thresholds.loss_count_min}-{thresholds.loss_count_max}"
    )
    parts.append("")
    parts.append(
        "**Aseist.** ja **Ostokyky** ovat pelaajalaskureita, ja niistä "
        "hävityn kierroksen jälkeinen luokka ratkeaa. **Bonus** on se "
        "häviöbonus, jolla ostokyky laskettiin; tyhjä tarkoittaa, ettei "
        "ehtoa B lasketa tällä kierroksella (jatkoaika tai puoliajan viimeinen "
        "kierros). **Jäljellä** on sen sijaan joukkueen keskiarvo eikä "
        "ratkaise mitään -- pelaajakohtaiset saldot ovat perustelussa."
    )
    parts.append("")
    parts.append(
        "Kaikki rahaluvut ovat dollareita per pelaaja ostoajan lopussa "
        "(freezetimen loppu + [parse].buy_window_seconds, katkaistuna "
        "kierroksen ensimmäiseen kuolemaan). "
        "**Käytössä** = jäljellä + käytetty eli se raha, joka joukkueella oli "
        "ostoaikana. **Jäljellä** on saldo ostojen jälkeen, joten "
        "säästökierroksella se on suuri. **Ostettu** on varustearvon kasvu "
        "kierroksen alusta ostoajan loppuun."
    )
    parts.append("")

    headers = [o for o, _ in ROUND_LIST_COLUMNS]
    parts.append("| " + " | ".join(headers) + " |")
    parts.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        parts.append("| " + " | ".join(_md(s) for s in round_list_cells(row)) + " |")

    parts.append("")
    parts.append("## Kierrostyypit")
    parts.append("")
    for value, label_fi in ROUND_TYPE_FI.items():
        parts.append(f"- `{value}` -- {label_fi}")
    parts.append(f"- `{UNCLASSIFIED}` -- havainto puuttui, kierrosta ei luokiteltu")
    parts.append("")
    parts.append(
        "`is_league` ja `roster_class` jäävät tässä vaiheessa tyhjiksi: ne "
        "tulevat joukkueindeksistä, joka syntyy vasta Epicissä 3."
    )
    parts.append("")
    return "\n".join(parts)


def _md(text: str) -> str:
    """Suojaa solun sisältö Markdown-taulukkoa varten.

    Putkimerkki katkaisisi solun ja rivinvaihto koko taulukon; backtick
    aloittaisi koodijakson, joka söisi loput rivistä. Kaikki kolme tulevat
    perusteluista, jotka ovat vapaata tekstiä.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "\\`")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


# -- Luvut ------------------------------------------------------------------------


def _stats(
    rows: list[dict[str, object]],
    team_key: str,
    list_rel: PurePosixPath,
    unnumbered: int,
) -> dict[str, object]:
    """Käyttäjälle näytettävät luvut.

    ``by_type`` sisältää vain oikeat kierrostyypit; luokittelemattomat ovat
    omana lukunaan, jotta niitä ei näytetä kahdesti.
    """
    distribution: dict[str, int] = {}
    unclassified = 0
    for row in rows:
        round_type = row["round_type"]
        if round_type is None:
            unclassified += 1
            continue
        key = str(round_type)
        distribution[key] = distribution.get(key, 0) + 1
    return {
        "team_key": team_key,
        "rounds": len(rows),
        "by_type": distribution,
        "unclassified": unclassified,
        "unnumbered": unnumbered,
        "round_list": str(list_rel),
        "rows": rows,
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
