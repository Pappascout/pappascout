"""``parse`` -- putken ensimmäinen vaihe: demosta kierrostaulu.

Vaihe lukee yhden demon portin takaa ja kirjoittaa arkistoon
``parsed/<map_demo_id>/rounds.parquet`` sekä sen manifestin. Taulussa on kaksi
riviä jokaista **pelattua** kierrosta kohden, yksi kummallekin joukkueelle, ja
kaikki arvot ovat demosta *havaittuja*: raha ja varustearvo freezetimen
lopussa, kierroksen alun varustearvo, eloonjääneet, voittaja ja voiton syy.
Kierrostyyppi, loss count ja muut johdokset syntyvät vasta ``classify``-
vaiheessa, joka laskee ne joka ajolla uudelleen.

Mitä tauluun päätyy
-------------------
Warmup-kierrokset, puukkokierros ja ``mp_restartgame``-nollaukset **eivät ole
pelattuja kierroksia**: ne eivät saa kierrosnumeroa eivätkä päädy tauluun.
``round_raw`` on demon oma kierroslaskuri, joten ohitetut kierrokset näkyvät
taulussa aukkona (Ancient: ``round_no`` 1..21 vastaa ``round_raw`` 2..22) --
niiden lukumäärä kerrotaan myös ajon yhteenvedossa. Päätöksen tekee yksi ainoa
funktio, :func:`~pappascout.domain.rounds.mark_played_rounds`, jota vain tämä
vaihe kutsuu.

Tarkistukset ennen kirjoitusta
------------------------------
Vaihe validoi sekä lukemansa että kirjoittamansa taulun (AD-2), ja lisäksi
kaksi asiaa, jotka pelkkä skeema ei näe:

* :func:`~pappascout.domain.rounds.check_win_reasons` -- CS2:n sääntö siitä,
  kuka voi voittaa kierroksen millä tavalla. Rikkomus tarkoittaa lähes aina,
  että puolet ovat menneet väärin päin.
* Rivimäärä on tasan kaksi per kierros. Yksi tai kolme riviä menisi skeemasta
  läpi mutta vääristäisi jokaisen myöhemmän joukkuekohtaisen summan.

Uudelleenajo
------------
Manifestin ``params_hash`` lasketaan **vain** ``[parse]``-osiosta, ja
``tool_versions`` sisältää vain demoparser2:n. ``[thresholds]``-arvon
muuttaminen ei siis voi invalidoida parsintaa -- se on koko AD-3:n
asetuspartition tarkoitus, ja tämä vaihe ei edes näe muita osioita.

Demon tiiviste luetaan sen omasta ``.meta.json``-tiedostosta. Jos sitä ei ole
(käsin arkistoon kopioitu demo), tunnisteena on tiedoston **koko ja
muokkausaika**: 233 MB:n sha256 jokaisella ajolla olisi hitaampi kuin itse
parsinta. Muokkausaika on mukana, koska pelkkä koko ei huomaa muuttunutta
demoa. Jos OneDrive-synkronointi muuttaa aikaleimaa, seurauksena on yksi turha
uudelleenparsinta -- vaarattomampi virhe kuin vanhentunut tulos. Manifestin voi
aina ohittaa lipulla ``force``.

Virhepolitiikka
---------------
Sääntö on yksi: **kelvollista tulosta ei koskaan ylikirjoiteta
epäonnistumisella.** Jos parsinta kaatuu ja arkistossa on jo ehjä
``rounds.parquet`` ja sen ``ok``-manifesti, molemmat jätetään koskematta.
Muussa tapauksessa kirjoitetaan ``parse_failed``-manifesti, joka estää
ohituksen ja kertoo syyn. Vajaata taulua ei synny kummassakaan tapauksessa,
koska kirjoitus on atominen ja tapahtuu vasta kaikkien tarkistusten jälkeen.
"""

from __future__ import annotations

import json
import time
from pathlib import Path, PurePosixPath

import polars as pl

from pappascout.adapters.protocols import ROUNDS_ADAPTER_COLUMNS, DemoRoundsParser
from pappascout.archive.atomic_write import atomic_path
from pappascout.archive.manifest import (
    Manifest,
    ManifestInput,
    compute_params_hash,
    tool_versions,
)
from pappascout.archive.paths import (
    DEMO_SUFFIXES,
    ArchivePaths,
    parsed_manifest,
    parsed_table,
    safe_component,
)
from pappascout.domain.models import ParseSettings
from pappascout.domain.rounds import check_win_reasons, mark_played_rounds
from pappascout.domain.schemas import ROUNDS, validate
from pappascout.errors import DemoUnavailable, ParseError, SchemaError
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "TABLE",
    "TOOLS",
    "run",
    "resolve_demo",
    "map_demo_id_from_path",
    "default_parser",
]

STAGE = "parse"
TABLE = "rounds"

#: Työkalut, joiden versio muuttaa tämän vaiheen tuloksen (manifest-moduulin
#: sääntö). Pappascoutin omaa versiota ei merkitä: korjauspäivitys ei saa
#: pakottaa koko arkiston uudelleenparsintaa.
TOOLS = ("demoparser2",)

#: Virheet, joista kirjataan yksikkökohtainen tila manifestiin (AD-9).
_TALLENNETTAVAT = (ParseError, SchemaError, OSError)


def default_parser() -> DemoRoundsParser:
    """Tuotannon demoparser2-toteutus.

    Tuonti on funktion sisällä, jotta tämän moduulin tuominen ei lataa
    demoparser2:ta -- vaihe itse tuntee vain portin, ja testit antavat sille
    feikin.
    """
    from pappascout.adapters.demo_parser import Demoparser2Rounds

    return Demoparser2Rounds()


def map_demo_id_from_path(path: Path) -> str:
    """Päättele ``map_demo_id`` demotiedoston nimestä.

    FACEITin tiedostonimi on tunniste sellaisenaan, joten pääte riisutaan ja
    loppu tarkistetaan polun osaksi kelpaavaksi.
    """
    name = Path(path).name
    for suffix in sorted(DEMO_SUFFIXES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    else:
        name = Path(name).stem
    return safe_component(name, "map_demo_id")


def resolve_demo(archive: ArchivePaths, target: str) -> tuple[str, Path]:
    """Tulkitse käyttäjän antama kohde tiedostoksi ja tunnisteeksi.

    Kohde saa olla joko polku demotiedostoon tai pelkkä ``map_demo_id``, jolloin
    demo etsitään arkiston ``demos/``- ja ``import/``-hakemistoista.

    Raises:
        DemoUnavailable: Jos demoa ei löydy. Viesti kertoo, mistä etsittiin.
    """
    candidate = Path(target).expanduser()
    if candidate.is_file():
        return map_demo_id_from_path(candidate), candidate

    map_demo_id = safe_component(target, "map_demo_id")
    loydetty = archive.find_demo(map_demo_id)
    if loydetty is not None:
        return map_demo_id, loydetty

    import_dir = archive.import_dir()
    for suffix in DEMO_SUFFIXES:
        polku = import_dir / f"{map_demo_id}{suffix}"
        if polku.is_file():
            return map_demo_id, polku

    etsityt = [str(archive.demo(map_demo_id, s)) for s in DEMO_SUFFIXES]
    etsityt += [str(import_dir / f"{map_demo_id}{s}") for s in DEMO_SUFFIXES]
    listaus = "\n".join(f"    {p}" for p in etsityt)
    raise DemoUnavailable(
        f"Demoa {map_demo_id} ei löytynyt.\n"
        "Etsin näistä poluista:\n"
        f"{listaus}\n"
        "Kopioi demo arkiston import-hakemistoon tai anna tiedoston polku "
        "suoraan komennolle."
    )


def _demo_fingerprint(archive: ArchivePaths, map_demo_id: str, demo_path: Path) -> str:
    """Demon tunniste manifestin syötelistaan.

    Ensisijaisesti ``demos/<id>.meta.json``-tiedoston ``sha256``; muuten
    tiedoston koko ja muokkausaika. Tiivistettä ei lasketa demosta uudelleen
    (ks. moduulin docstring).

    Raises:
        DemoUnavailable: Jos tiedoston tietoja ei saada luettua. Yhteinen
            varakonstantti olisi vaarallinen: kaksi eri lukukelvotonta demoa
            saisi saman tunnisteen, jolloin toisen tulos näyttäisi toisen
            ajantasaiselta tulokselta.
    """
    meta_path = archive.demo_meta(map_demo_id)
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        sha = meta.get("sha256")
        if isinstance(sha, str) and sha:
            return sha
    try:
        stat = demo_path.stat()
    except OSError as exc:
        raise DemoUnavailable(
            f"Demon {demo_path} tietoja ei voitu lukea: {exc}\n"
            "Tarkista, ettei tiedosto ole OneDriven pilvipaikkamerkki (avaa se "
            "kerran Resurssienhallinnassa) ja että polku on oikea."
        ) from exc
    return f"size-{stat.st_size}-mtime-{stat.st_mtime_ns}"


def _params_hash(settings: ParseSettings) -> str:
    return compute_params_hash(settings.model_dump(mode="json"))


def _check_port_columns(df: pl.DataFrame) -> None:
    """Tarkista adapterin tuottama taulu ennen jatkokäsittelyä.

    Vaatimus on **täsmällinen sarakejoukko**, ei osajoukko: ylimääräinen sarake
    tarkoittaa, että portin toteutus ja sopimus ovat erkaantuneet, ja se
    päätyisi hiljaa mukaan tai kaatuisi vasta domain-kerroksessa.
    """
    saadut = set(df.columns)
    odotetut = set(ROUNDS_ADAPTER_COLUMNS)
    puuttuvat = sorted(odotetut - saadut)
    ylimaaraiset = sorted(saadut - odotetut)
    if not puuttuvat and not ylimaaraiset:
        return
    osat = []
    if puuttuvat:
        osat.append(f"puuttuu: {', '.join(puuttuvat)}")
    if ylimaaraiset:
        osat.append(f"ylimääräisiä: {', '.join(ylimaaraiset)}")
    raise SchemaError(
        f"Demoportti palautti sopimuksen vastaisen kierrostaulun -- "
        f"{'; '.join(osat)}.\n"
        "Portin sopimus on adapters/protocols.py:n ROUNDS_ADAPTER_COLUMNS."
    )


def _check_two_rows_per_round(df: pl.DataFrame) -> None:
    """Jokaisella kierroksella on oltava tasan kaksi riviä.

    Taulu on pitkä: yksi rivi kummallekin joukkueelle. Kolmas rivi tai
    puuttuva pari läpäisisi skeeman mutta kaksinkertaistaisi tai puolittaisi
    joukkuekohtaiset summat kaikessa myöhemmässä analyysissa.
    """
    kierroksia = df["round_no"].n_unique()
    if df.height == 2 * kierroksia:
        return
    poikkeavat = (
        df.group_by("round_no")
        .len()
        .filter(pl.col("len") != 2)
        .sort("round_no")
        .head(5)
    )
    raise SchemaError(
        f"Kierrostaulussa on {df.height} riviä {kierroksia} kierrokselle; "
        "pitäisi olla tasan kaksi riviä per kierros (yksi kummallekin "
        "joukkueelle).\n"
        f"Poikkeavat kierrokset: {poikkeavat.to_dicts()}"
    )


def _stats(df: pl.DataFrame, ohitetut: int = 0) -> dict[str, object]:
    """Käyttäjälle näytettävät luvut valmiista taulusta."""
    if df.is_empty():
        return {
            "rounds": 0,
            "rows": 0,
            "max_round_no": 0,
            "skipped_rounds": ohitetut,
            "no_freeze_end": 0,
        }
    return {
        "rounds": int(df["round_no"].n_unique()),
        "rows": int(df.height),
        "max_round_no": int(df["round_no"].max() or 0),
        "skipped_rounds": ohitetut,
        "no_freeze_end": int(
            df.filter(pl.col("status") == "no_freeze_end")["round_no"].n_unique()
        ),
    }


def _existing_stats(table_abs: Path) -> dict[str, object]:
    """Luvut ohitettuun ajoon: luetaan valmis taulu, ei parsita demoa.

    Jos tulosta ei saada luettua, palautetaan tieto siitä eikä nollia --
    nollarivi näyttäisi siltä, että demossa ei ollut yhtään kierrosta.
    """
    try:
        return _stats(pl.read_parquet(table_abs))
    except (OSError, pl.exceptions.PolarsError) as exc:
        return {"unreadable": f"{type(exc).__name__}: {exc}"}


def run(
    settings: ParseSettings,
    archive: ArchivePaths,
    map_demo_id: str,
    parser: DemoRoundsParser,
    *,
    demo_path: Path | None = None,
    force: bool = False,
) -> StageResult:
    """Parsi yksi demo kierrostauluksi.

    Args:
        settings: ``[parse]``-osio -- ainoa osio, jonka tämä vaihe näkee.
        archive: Arkiston polut.
        map_demo_id: Yksikön tunniste, ``{match_id}-{map_index}``.
        parser: Demoportti (AD-8). Tuotannossa :func:`default_parser`.
        demo_path: Demotiedosto. Oletuksena etsitään arkistosta.
        force: Ohita manifestin täsmäys ja parsi joka tapauksessa.

    Returns:
        :class:`~pappascout.stages.StageResult`, jossa ``stats`` kertoo
        kierrosten määrän, rivimäärän, suurimman kierrosnumeron sekä
        ohitettujen ja ankkurittomien kierrosten lukumäärän.

    Raises:
        DemoUnavailable: Jos demoa ei löydy tai sitä ei voi lukea.
        ~pappascout.errors.ParseError: Jos demo ei ole luettavissa tai sen
            sisältö rikkoo CS2:n sääntöjä.
        ~pappascout.errors.SchemaError: Jos tulos ei vastaa sopimusta.
    """
    aloitus = time.perf_counter()
    map_demo_id = safe_component(map_demo_id, "map_demo_id")

    if demo_path is None:
        _, demo_path = resolve_demo(archive, map_demo_id)
    demo_path = Path(demo_path)

    table_rel = parsed_table(map_demo_id, TABLE)
    manifest_rel = parsed_manifest(map_demo_id)
    table_abs = archive.resolve(table_rel)
    manifest_abs = archive.resolve(manifest_rel)

    result_id = str(PurePosixPath("parsed") / map_demo_id)
    inputs = [
        ManifestInput(
            result_id=f"demo/{map_demo_id}",
            sha256=_demo_fingerprint(archive, map_demo_id, demo_path),
        )
    ]
    params_hash = _params_hash(settings)
    versiot = tool_versions(*TOOLS)

    olemassa = Manifest.read_if_exists(manifest_abs)
    if (
        not force
        and olemassa is not None
        and olemassa.is_current(
            inputs=inputs,
            params_hash=params_hash,
            tool_versions=versiot,
            root=archive.root,
        )
    ):
        return StageResult(
            stage=STAGE,
            unit=map_demo_id,
            status="ok",
            skipped=True,
            outputs=tuple(PurePosixPath(o) for o in olemassa.outputs),
            manifest_path=manifest_rel,
            reason=(
                "Tulos on ajan tasalla: manifesti täsmää eikä demoa tarvitse "
                "parsia uudelleen."
            ),
            duration_s=time.perf_counter() - aloitus,
            stats=_existing_stats(table_abs),
        )

    try:
        df, ohitetut = _parse_table(parser, demo_path, map_demo_id)
    except _TALLENNETTAVAT as exc:
        _record_failure(
            archive=archive,
            manifest_abs=manifest_abs,
            table_abs=table_abs,
            olemassa=olemassa,
            result_id=result_id,
            params_hash=params_hash,
            inputs=inputs,
            versiot=versiot,
            reason=str(exc),
        )
        raise

    with atomic_path(table_abs) as tmp:
        df.write_parquet(tmp)

    Manifest.new(
        result_id=result_id,
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=versiot,
        status="ok",
        outputs=(str(table_rel),),
    ).write(manifest_abs)

    stats = _stats(df, ohitetut)
    diagnostics = getattr(parser, "diagnostics", None)
    if diagnostics is not None:
        stats["tick_rate"] = diagnostics.tick_rate
        stats["tick_rate_measured"] = diagnostics.tick_rate_measured

    return StageResult(
        stage=STAGE,
        unit=map_demo_id,
        status="ok",
        skipped=False,
        outputs=(table_rel,),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - aloitus,
        stats=stats,
    )


def _parse_table(
    parser: DemoRoundsParser, demo_path: Path, map_demo_id: str
) -> tuple[pl.DataFrame, int]:
    """Lue demo ja rakenna valmis, tarkistettu ``ROUNDS``-taulu.

    Returns:
        ``(taulu, ohitettujen kierrosten määrä)``.
    """
    raaka = parser.parse_rounds(demo_path)
    _check_port_columns(raaka)

    numeroitu = mark_played_rounds(raaka)
    ohitetut = int(
        numeroitu.filter(pl.col("round_no").is_null())["round_raw"].n_unique()
    )
    pelatut = numeroitu.filter(pl.col("round_no").is_not_null())

    df = pelatut.select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in ROUNDS if name != "map_demo_id"],
    ).sort("round_no", "side")

    if df.is_empty():
        raise ParseError(
            f"Demosta {demo_path.name} ei löytynyt yhtään pelattua kierrosta "
            f"({ohitetut} kierrosrajaa oli warmupia, puukkokierros tai "
            "uudelleenkäynnistys).\n"
            "Tyhjää tulosta ei kirjoiteta -- muuten se jäisi manifestin "
            "perusteella pysyvästi ohitetuksi. Tarkista, että demo on koko "
            "ottelun tallenne."
        )

    validate(df, ROUNDS, TABLE)
    check_win_reasons(df)
    _check_two_rows_per_round(df)
    return df, ohitetut


def _record_failure(
    *,
    archive: ArchivePaths,
    manifest_abs: Path,
    table_abs: Path,
    olemassa: Manifest | None,
    result_id: str,
    params_hash: str,
    inputs: list[ManifestInput],
    versiot: dict[str, str],
    reason: str,
) -> None:
    """Kirjaa epäonnistuminen manifestiin -- mutta älä tuhoa ehjää tulosta.

    Jos arkistossa on jo kelvollinen taulu ja sen ``ok``-manifesti, molemmat
    jätetään paikalleen. Muuten manifestiin kirjataan ``parse_failed``, joka
    sekä kertoo syyn että estää ohituksen seuraavalla ajolla.
    """
    ehja_tulos = (
        olemassa is not None
        and olemassa.status == "ok"
        and olemassa.outputs_present(archive.root)
        and table_abs.is_file()
    )
    if ehja_tulos:
        return
    Manifest.new(
        result_id=result_id,
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=versiot,
        status="parse_failed",
        reason=reason,
        outputs=(),
    ).write(manifest_abs)
