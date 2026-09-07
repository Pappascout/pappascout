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

``team_key`` tässä vaiheessa
---------------------------
Subjekti valitaan ``--team``-valinnalla suoraan **kokoonpanotunnisteella**
(``lineup_key``), ja sitä käytetään myös hakemistonimenä. Kanoninen
``team_key`` on eri tunniste: se syntyy ``discover``issa ja nimeää
joukkueindeksin ja valintatiedostot. Hakemistoja ei nimetä uudelleen tässä
vaiheessa, joten kaksi tunnistetta elää rinnakkain ja niiden välinen silta on
``index/teams.json``in ``lineup_keys``-kenttä.

``is_league`` ja ``roster_class`` luetaan, ei lasketa
----------------------------------------------------
Molemmat kuvaavat **ottelua** eivätkä kierrosta, ja molemmat on jo laskettu:
``select`` kirjoittaa ne joukkueen valintatiedostoon
(``index/selections/<team_key>.json``). Tämä vaihe on niiden **lukija** --
:func:`read_match_facts` etsii käsiteltävän demon rivin, ja arvot latotaan
sellaisinaan jokaiselle kierrosriville. Sama päättely kahdessa paikassa olisi
kaksi totuutta, joten ``is_league``ia ei päätellä ``competition_id``:stä eikä
``roster_class``ia rosterikynnyksistä täällä.

**Arvoa ei arvata, eikä syy jää sanomatta.** Sarake jää tyhjäksi viidestä eri
syystä -- joukkueindeksiä ei ole, kokoonpanolla ei ole omistajaa, omistajalla
ei ole valintatiedostoa, demolle ei ole riviä, tai osumat ovat kentästä eri
mieltä -- ja jokainen niistä nimetään :attr:`MatchFacts.note`ssa, jonka
``run`` vie ``StageResult.reason``iin (AD-9). Ilman sitä rikkoutunut silta
näyttäisi raportissa täsmälleen samalta kuin käsin tuotu demo, jonka oikea
arvo on tyhjä: raportin ``unknown``-lokero on lokero eikä virhetila, ja arvaus
("luultavasti liigaottelu") olisi väärä väite datasta.

**Konsensus on kentittäin.** Kokoonpanon voi omistaa useampi joukkue, ja
samassa tiedostossa voi olla kaksi riviä samalle demolle. ``is_league`` kuvaa
ottelua (AD-10), joten kaikkien osumien on oltava siitä samaa mieltä;
``roster_class`` arvioidaan **kyseisen joukkueen** vakirosteria vasten (AD-6),
joten kaksi omistajaa saa siitä eri arvon normaalisti. Erimielinen kenttä jää
tyhjäksi -- toinen ei -- eikä kumpaakaan ratkaista arpomalla.

**Kytkentä ei ole manifestin syötteessä, ja vanhentuminen sanotaan ääneen.**
Valintatiedosto **ei** ole tämän vaiheen manifestin ``inputs``issa: yksi uusi
``select``-ajo pakottaisi muuten koko arkiston uudelleenluokitteluun. Hinta on
se, että valmis taulu voi kantaa vanhaa arvoa, joten ohitettu ajo
**varoittaa**, kun valintatiedosto on kirjoitettu tämän luokittelun jälkeen
(:func:`selection_staleness_note`), ja neuvoo ``--pakota``. Pelkkä
dokumentoitu sääntö jäisi ihmisen muistin varaan; varoitus tekee hiljaisesta
väärästä luvusta näkyvän.

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
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

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
from pappascout.stages.discover import read_teams_index, teams_from_index
from pappascout.stages.select import read_selection

__all__ = [
    "STAGE",
    "TABLE",
    "TOOLS",
    "ROUND_LIST_COLUMNS",
    "MatchFacts",
    "run",
    "resolve_team",
    "team_keys",
    "read_match_facts",
    "selection_staleness_note",
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
        ~pappascout.errors.PappascoutError: Jos demoa ei ole parsittu, ``team``
            ei täsmää kumpaankaan kokoonpanoon tai joukkueindeksi taikka
            valintatiedosto on rikki. **Puuttuva** valintatiedosto ei ole
            virhe: silloin ``is_league`` ja ``roster_class`` jäävät tyhjiksi.
        ~pappascout.errors.SchemaError: Jos kierrostaulu tai tulos ei vastaa
            sopimusta, tai jos valintatiedoston ``roster_class`` ei kelpaa
            ``CLASSIFIED``-skeeman enumiin.
    """
    started = time.perf_counter()
    map_demo_id = safe_component(map_demo_id, "map_demo_id")

    rounds, unnumbered = _read_rounds(archive, map_demo_id)
    parse_manifest = _read_parse_manifest(archive, map_demo_id)
    # **Kokoonpanotunniste, ei kanoninen ``team_key``.** Arkiston hakemisto on
    # nimetty tästä (``paths.classified``in parametri on historiallisista
    # syistä ``team_key``), mutta arvo on ``lineup_key`` -- ja juuri siksi
    # valintatiedosto haetaan joukkueindeksin kautta eikä tällä nimellä.
    lineup_key = resolve_team(rounds, team, map_demo_id)

    table_rel = classified(lineup_key, map_demo_id)
    list_rel = classified_round_list(lineup_key, map_demo_id)
    manifest_rel = classified_manifest(lineup_key, map_demo_id)
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
            reason=_skip_reason(archive, lineup_key, existing),
            duration_s=time.perf_counter() - started,
            stats=_stats(
                round_list_rows(ready), lineup_key, list_rel, unnumbered
            ),
        )

    # **Luetaan vasta tässä, ohitushaaran jälkeen.** Ohitettu ajo ei lue
    # arvoja lainkaan, joten yksi rikkinäinen valintatiedosto ei muuta koko
    # arkiston valmiita luokitteluja virheiksi. Siirto funktion alkuun olisi
    # juuri se regressio; ``test_a_broken_selection_file_does_not_break_a_skipped_run``
    # pinnaa järjestyksen.
    facts = read_match_facts(archive, lineup_key, map_demo_id)

    df, rows = classify_rounds(
        rounds, lineup_key, thresholds, map_demo_id, economy=economy, facts=facts
    )

    with atomic_path(table_abs) as tmp:
        df.write_parquet(tmp)
    atomic_write_text(
        list_abs,
        render_round_list_markdown(
            rows,
            map_demo_id=map_demo_id,
            team_key=lineup_key,
            thresholds=thresholds,
            league=league,
            economy=economy,
        ),
    )

    Manifest.new(
        result_id=str(PurePosixPath("classified") / lineup_key / map_demo_id),
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
        # AD-9: tulos on ``ok`` myös silloin kun kaksi saraketta jäivät
        # tyhjiksi, mutta **syy ei jää sanomatta**. Ilman tätä riviä
        # rikkoutunut silta näyttäisi raportissa täsmälleen samalta kuin
        # käsin tuotu demo.
        reason=facts.note,
        duration_s=time.perf_counter() - started,
        stats=_stats(rows, lineup_key, list_rel, unnumbered),
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

    Luetaan kierrostaulusta eikä joukkueindeksistä: tunniste on tässä
    vaiheessa kokoonpanotunniste, ja demon molemmat kokoonpanot ovat
    kierrostaulussa riippumatta siitä, tunnetaanko niiden joukkueet.
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


# -- Ottelutosiasiat valintatiedostosta -----------------------------------------


class MatchFacts(NamedTuple):
    """Demokohtaiset ottelutosiasiat, jotka ``select`` on jo laskenut.

    Oletusarvo on **tyhjä molemmilta**, ja se on rehellinen tila eikä
    puuttuva: käsin tuodulla demolla ei ole valintariviä, eikä kumpaakaan
    arvoa voi silloin tietää.

    ``note`` kertoo **miksi** arvo puuttuu. Ilman sitä viisi eri tilannetta --
    indeksi puuttuu, kokoonpanolla ei omistajaa, omistajalla ei
    valintatiedostoa, demolle ei riviä, rivit eri mieltä -- näyttäisivät
    raportissa täsmälleen samalta, ja rikkoutunut silta olisi erottamaton
    käsin tuodusta demosta. ``run`` vie sen ``StageResult.reason``iin (AD-9).
    """

    #: Onko ottelu asetusten championshipeissa. ``None`` = ei tiedossa.
    is_league: bool | None = None
    #: Rosterikynnyksen luokka, ``CLASSIFIED``-skeeman enumin arvo.
    roster_class: str | None = None
    #: Suomenkielinen syy vajaalle tulokselle, tai ``None`` kun molemmat saatiin.
    note: str | None = None


def roster_classes() -> tuple[str, ...]:
    """Kelvolliset rosteriluokat **``CLASSIFIED``-skeeman enumista**.

    Luettelo johdetaan siitä sopimuksesta, jota vasten arvo lopulta
    kirjoitetaan, eikä rinnakkaisesta vakiosta
    (:data:`~pappascout.constants.ROSTER_CLASSES`). Kaksi lähdettä voisivat
    erkaantua, ja silloin tarkistus ja virheilmoitus puhuisivat eri joukosta
    kuin Polars: viesti sanoisi "ei kelpaa skeemaan" arvosta, joka kelpaa --
    tai päästäisi läpi arvon, joka kaataa kirjoituksen kolme riviä myöhemmin.
    """
    return tuple(str(value) for value in CLASSIFIED["roster_class"].categories)


def read_match_facts(
    archive: ArchivePaths, lineup_key: str, map_demo_id: str
) -> MatchFacts:
    """Etsi demon ``is_league`` ja ``roster_class`` valintatiedostosta.

    Arvoja **ei lasketa täällä**: ``select`` on niiden ainoa laskija, ja tämä
    on lukija. Reitti on kaksivaiheinen, koska tunnisteita on kaksi: tämän
    vaiheen hakemistonimi on kokoonpanotunniste, kun taas valintatiedosto on
    nimetty kanonisella ``team_key``:llä. Silta on ``index/teams.json``in
    ``lineup_keys``, ja omistajat päätellään siitä kentästä lukemalla -- se on
    ainoa kohta, jossa käännös tehdään.

    **Konsensus on kentittäin eikä tietueena.** Kokoonpanon voi omistaa
    useampi joukkue, ja samassa tiedostossa voi olla kaksi riviä samalle
    demolle; kaikki osumat luetaan ja kumpikin kenttä ratkaistaan erikseen.
    Ero on olennainen: ``is_league`` kuvaa **ottelua** (AD-10), joten kaikkien
    osumien on oltava siitä samaa mieltä, kun taas ``roster_class`` arvioidaan
    **kyseisen joukkueen** vakirosteria vasten (AD-6), joten kaksi omistajaa
    saa siitä eri arvon täysin normaalisti. Tietuetasoinen vertailu heittäisi
    yksimielisen ``is_league``in pois vain siksi, että luokat erosivat.

    Kun kenttä on erimielinen, se jää **tyhjäksi** ja syy kirjataan
    ``note``en: kiistaa ei ratkaista arpomalla, ja "ensimmäinen voittaa" olisi
    juuri se arpominen.

    Args:
        archive: Arkiston polut.
        lineup_key: Subjektin **kokoonpanotunniste**, sama jolla tulos
            kirjoitetaan. Ei kanoninen ``team_key``.
        map_demo_id: Käsiteltävän demon tunniste.

    Returns:
        :class:`MatchFacts`. Kun arvo puuttuu, ``note`` nimeää syyn.

    Raises:
        ~pappascout.errors.PappascoutError: Jos joukkueindeksi tai
            valintatiedosto on olemassa mutta rikki. **Puuttuva** tiedosto ei
            ole virhe, vaan tuntematon arvo.
        ~pappascout.errors.SchemaError: Jos rivin ``is_league`` tai
            ``roster_class`` ei kelpaa ``CLASSIFIED``-skeemaan.
    """
    if not archive.teams_index().is_file():
        return MatchFacts(
            note=(
                "Joukkueindeksiä ei ole, joten ottelun lajia ja rosteriluokkaa "
                "ei voitu lukea (is_league ja roster_class jäivät tyhjiksi).\n"
                "Aja halutessasi ensin: uv run pappascout discover"
            )
        )

    owners = _owners(archive, lineup_key)
    if not owners:
        return MatchFacts(
            note=(
                f"Kokoonpanoa {lineup_key} ei omista yksikään joukkueindeksin "
                "joukkue, joten valintatiedostoa ei voitu paikantaa "
                "(is_league ja roster_class jäivät tyhjiksi). Käsin tuodulla "
                "demolla tämä on odotettua."
            )
        )

    hits: list[tuple[str, MatchFacts]] = []
    missing_file: list[str] = []
    for team_key in owners:
        if not archive.selection(team_key).is_file():
            missing_file.append(team_key)
            continue
        for row in _selection_rows(archive, team_key, map_demo_id):
            hits.append((team_key, _match_facts(row, team_key, map_demo_id)))

    if not hits:
        if len(missing_file) == len(owners):
            return MatchFacts(
                note=(
                    "Joukkueelle "
                    f"{', '.join(missing_file)} ei ole valintatiedostoa, joten "
                    "is_league ja roster_class jäivät tyhjiksi.\n"
                    "Aja ensin: uv run pappascout select --team "
                    f'"{missing_file[0]}"'
                )
            )
        return MatchFacts(
            note=(
                f"Demoa {map_demo_id} ei ole joukkueen "
                f"{', '.join(k for k in owners if k not in missing_file)} "
                "valintatiedostossa, joten is_league ja roster_class jäivät "
                "tyhjiksi. Käsin tuodulla demolla tämä on odotettua."
            )
        )

    is_league, league_note = _consensus(
        [(team_key, facts.is_league) for team_key, facts in hits],
        field="is_league",
        why=(
            "is_league kuvaa ottelua eikä joukkuetta, joten kahta eri arvoa ei "
            "voi olla oikein"
        ),
    )
    roster_class, class_note = _consensus(
        [(team_key, facts.roster_class) for team_key, facts in hits],
        field="roster_class",
        why=(
            "rosteriluokka arvioidaan joukkueen omaa vakirosteria vasten, joten "
            "eri arvot ovat odotettuja eikä niistä voi valita yhtä"
        ),
    )
    notes = [note for note in (league_note, class_note) if note]
    return MatchFacts(
        is_league=is_league,
        roster_class=roster_class,
        note=" ".join(notes) if notes else None,
    )


def _owners(archive: ArchivePaths, lineup_key: str) -> list[str]:
    """Joukkueindeksin joukkueet, jotka omistavat tämän kokoonpanon.

    Käännös tehdään **tässä ja vain tässä**, jotta jokainen lukija saa saman
    vastauksen. Omistajuus luetaan ``lineup_keys``-kentästä, koska se on se
    kenttä, jonka ``discover`` kirjoittaa jokaiselle joukkueelle
    (``domain.teams.assign_lineup_keys``). Indeksin
    ``contested_lineup_keys``-luetteloa ei tarvita eikä luettaisi: se on
    ``discover``in raportti samasta havainnosta, ja kahden lähteen sijaan
    kysytään sitä, joka kertoo **ketkä** omistajat ovat.
    """
    return [
        team.team_key
        for team in teams_from_index(read_teams_index(archive))
        if lineup_key in team.lineup_keys
    ]


def _consensus(
    values: list[tuple[str, object]], *, field: str, why: str
) -> tuple[Any, str | None]:
    """Yksi arvo, jos kaikki osumat ovat samaa mieltä -- muuten tyhjä ja syy.

    Args:
        values: ``(team_key, arvo)`` jokaisesta löytyneestä valintarivistä.
        field: Kentän nimi virheilmoitukseen.
        why: Miksi erimielisyys on juuri tässä kentässä sitä mitä se on.

    Returns:
        ``(arvo, huomio)``. Huomio on ``None``, kun arvo saatiin.
    """
    distinct = {value for _, value in values}
    if len(distinct) == 1:
        return next(iter(distinct)), None
    listed = ", ".join(
        f"{team_key}: {value!r}" for team_key, value in sorted(values, key=str)
    )
    return None, (
        f"Valintariveillä on {len(distinct)} eri arvoa kentässä {field} "
        f"({listed}), joten se jäi tyhjäksi -- {why}."
    )


def _selection_rows(
    archive: ArchivePaths, team_key: str, map_demo_id: str
) -> list[dict[str, Any]]:
    """Demon **kaikki** rivit joukkueen valintatiedostosta.

    Kaikki eikä ensimmäinen: kahdennettu rivi ratkeaisi muuten hiljaisella
    "ensimmäinen voittaa" -säännöllä, ja se on sama arpominen, joka
    kiistanalaisilla kokoonpanoilla nimenomaan kiellettiin. Kahdennus menee
    samaan konsensukseen kuin kaksi omistajaa.

    Raises:
        PappascoutError: Jos tiedosto ei ole luettavissa tai sen muoto on
            tuntematon (:func:`~pappascout.stages.select.read_selection`), tai
            jos ``selections`` ei ole luettelo. Sama viesti ja sama neuvo kuin
            ``fetch``issä: aja ``select`` uudelleen.
    """
    document = read_selection(archive, team_key)
    rows = document.get("selections")
    if not isinstance(rows, list):
        raise PappascoutError(
            f"Joukkueen {team_key} valintatiedostossa ei ole "
            "selections-listaa.\n"
            f'Aja uudelleen: uv run pappascout select --team "{team_key}"'
        )
    return [
        row
        for row in rows
        if isinstance(row, dict) and row.get("map_demo_id") == map_demo_id
    ]


def _match_facts(
    row: dict[str, Any], team_key: str, map_demo_id: str
) -> MatchFacts:
    """Poimi kaksi kenttää valintariviltä ja tarkista, että ne kelpaavat.

    Tarkistus on tässä eikä vasta Polarsin tyypityksessä: vieras arvo
    kaatuisi muuten ``pl.Enum``in sisällä viestillä, joka ei kerro mistä
    tiedostosta se tuli. ``roster_ok`` **ei vaikuta**: hylkäys on otannan
    asia, eivät nämä kaksi tosiasiaa ottelusta.

    Raises:
        SchemaError: Jos arvo ei kelpaa ``CLASSIFIED``-skeemaan.
    """
    is_league = row.get("is_league")
    roster_class = row.get("roster_class")
    if is_league is not None and not isinstance(is_league, bool):
        raise SchemaError(
            f"Joukkueen {team_key} valintatiedoston rivillä {map_demo_id} on "
            f"is_league-arvo {is_league!r}, joka ei ole totuusarvo.\n"
            f'Aja uudelleen: uv run pappascout select --team "{team_key}"'
        )
    allowed = roster_classes()
    if roster_class is not None and roster_class not in allowed:
        raise SchemaError(
            f"Joukkueen {team_key} valintatiedoston rivillä {map_demo_id} on "
            f"roster_class-arvo {roster_class!r}, joka ei ole "
            f"CLASSIFIED-skeeman luokka ({', '.join(allowed)}).\n"
            f'Aja uudelleen: uv run pappascout select --team "{team_key}"'
        )
    return MatchFacts(is_league=is_league, roster_class=roster_class)


#: Ohituksen syy ilman vanhentumisvaroitusta.
SKIP_REASON = (
    "Tulos on ajan tasalla: manifesti täsmää eikä kierroksia tarvitse "
    "luokitella uudelleen."
)


def _skip_reason(
    archive: ArchivePaths, lineup_key: str, manifest: Manifest
) -> str:
    note = selection_staleness_note(archive, lineup_key, manifest)
    return SKIP_REASON if note is None else f"{SKIP_REASON} {note}"


def selection_staleness_note(
    archive: ArchivePaths, lineup_key: str, manifest: Manifest
) -> str | None:
    """Varoitus, jos valintatiedosto on uudempi kuin valmis luokittelu.

    Valintatiedosto **ei** ole manifestin syöte, joten sen muuttuminen ei
    invalidoi tulosta -- eikä siis myöskään kerro itsestään. Vanhentuminen
    olisi ilman tätä dokumentoitu muttei havaittavissa mistään: taulu kantaisi
    vanhaa ``is_league``ia, ja raportti näyttäisi ajan tasalla olevalta. Halvin
    korjaus on **sanoa se ohituksen syyssä** ja neuvoa ``--pakota``.

    **Ei nosta koskaan.** Ohitettu ajo ei lue arvoja lainkaan, joten
    rikkinäinen valintatiedosto ei saa muuttaa valmista tulosta virheeksi;
    lukukelvoton tiedosto tarkoittaa tässä vain sitä, ettei vanhentumisesta
    voi sanoa mitään.

    Returns:
        Varoitus, tai ``None`` jos tiedosto on vanhempi, sitä ei ole tai sen
        aikaleimaa ei voi lukea.
    """
    newest: datetime | None = None
    whose: str | None = None
    try:
        owners = _owners(archive, lineup_key) if archive.teams_index().is_file() else []
        for team_key in owners:
            if not archive.selection(team_key).is_file():
                continue
            moment = _moment(read_selection(archive, team_key).get("generated_at"))
            if moment is not None and (newest is None or moment > newest):
                newest, whose = moment, team_key
    except PappascoutError:
        return None

    if newest is None or newest <= manifest.created_at:
        return None
    return (
        f"Huomio: joukkueen {whose} valintatiedosto on kirjoitettu "
        f"{newest.isoformat()}, tämä luokittelu {manifest.created_at.isoformat()}. "
        "Valintatiedosto ei ole tämän vaiheen syöte, joten is_league ja "
        "roster_class voivat olla vanhentuneita -- aja uudelleen lipulla "
        "--pakota, jos haluat ne tiedoston mukaan."
    )


def _moment(value: object) -> datetime | None:
    """ISO-aikaleima aikavyöhykkeellisenä, tai ``None`` jos sitä ei voi lukea."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# -- Luokittelu ------------------------------------------------------------------


def classify_rounds(
    rounds: pl.DataFrame,
    team_key: str,
    thresholds: ThresholdSettings,
    map_demo_id: str,
    *,
    economy: EconomySettings,
    facts: MatchFacts,
) -> tuple[pl.DataFrame, list[dict[str, object]]]:
    """Rakenna ``CLASSIFIED``-taulu ja kierroslistan rivit kierrostaulusta.

    Julkinen, koska tämä on vaiheen koko päättely ilman tiedostoja: sen voi
    ajaa suoraan sekä käsin rakennetulla taululla että oikean demon
    kierrostaululla ilman arkistoa.

    Kumpikin joukkue luokitellaan omilla loss counteillaan ja omalla
    kierroshistoriallaan; subjektin rivi saa vastustajan tyypin
    ``opp_round_type``-sarakkeeseen.

    Args:
        facts: Demokohtaiset ottelutosiasiat, jotka :func:`read_match_facts`
            luki valintatiedostosta. **Ne latotaan sellaisinaan jokaiselle
            riville** eikä niitä lasketa täällä; ``is_league`` kuvaa ottelua
            eikä kierrosta, ja ``domain.aggregate`` kaataa ajon, jos yhden
            demon kierroksilla olisi kaksi eri arvoa.

            **Pakollinen eikä oletukseltaan tyhjä.** Oletus tekisi
            unohtamisesta hiljaisen: kutsuja, joka ei anna faktoja, tuottaisi
            täsmälleen sen tyhjän sarakkeen, jonka korjaamisesta tämä koodi
            on. Tiedostoton kutsuja antaa ``MatchFacts()`` ja sanoo silloin
            ääneen, ettei se tiedä arvoja.
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
                # Luettu, ei laskettu: arvo tulee ``select``in
                # valintatiedostosta ja on demokohtainen, joten sama arvo
                # jokaiselle riville. Tyhjä, kun riviä ei ollut.
                "is_league": facts.is_league,
                "roster_class": facts.roster_class,
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
