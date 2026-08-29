"""``parse`` -- putken ensimmäinen vaihe: demosta kolme taulua.

Vaihe lukee yhden demon portin takaa ja kirjoittaa arkistoon
``parsed/<map_demo_id>/rounds.parquet``, ``.../ticks.parquet``,
``.../events.parquet`` sekä niiden yhteisen manifestin.

``rounds`` on kaksi riviä jokaista **pelattua** kierrosta kohden, yksi
kummallekin joukkueelle, ja kaikki arvot ovat demosta *havaittuja*: raha ja
varustearvo freezetimen lopussa, kierroksen alun varustearvo, eloonjääneet,
voittaja ja voiton syy. Kierrostyyppi, loss count ja muut johdokset syntyvät
vasta ``classify``-vaiheessa, joka laskee ne joka ajolla uudelleen.

``ticks`` on rivi per (pelaaja, kierros, näytepiste): alue, koordinaatit ja
elossaolo muutamassa hetkessä kierroksen alusta (``[parse].snapshot_seconds``)
sekä ensikontaktin hetkellä. Kymmenen pelaajaa tallentuu joka näytepisteessä
``is_alive``-lipulla; kuolleiden suodatus ja aggregointi ovat myöhempien
vaiheiden työtä (AD-10).

``events`` on rivi per utility-tapahtuma: heitto ja räjähdys ovat kaksi riviä,
jotka yhdistää ``(round_no, grenade_entity_id)``. Utility mitataan **heitoista,
ei ostoista** -- utilityä dropataan, joten ostaja ja heittäjä voivat olla eri
pelaajat. **Tyhjä tapahtumataulu on kelvollinen tulos**, toisin kuin tyhjä
kierros- tai näytepistetaulu: demossa on voitu jättää utility heittämättä,
mutta pelattuja kierroksia ja asetelmia siinä on aina.

Mitä tauluihin päätyy
---------------------
Warmup-kierrokset, puukkokierros ja ``mp_restartgame``-nollaukset **eivät ole
pelattuja kierroksia**: ne eivät saa kierrosnumeroa eivätkä päädy tauluun.
``round_raw`` on demon oma kierroslaskuri, joten ohitetut kierrokset näkyvät
taulussa aukkona (Ancient: ``round_no`` 1..21 vastaa ``round_raw`` 2..22) --
niiden lukumäärä kerrotaan myös ajon yhteenvedossa. Päätöksen tekee yksi ainoa
funktio, :func:`~pappascout.domain.rounds.mark_played_rounds`, jota vain tämä
vaihe kutsuu.

Sama päätös rajaa myös näytepisteet ja utility-tapahtumat: adapteri tuottaa
rivejä kaikilta ankkuroiduilta kierrosrajoilta, ja vaihe pudottaa
numeroimattomien kierrosten rivit samalla, kun se liittää ``round_no``:n
avaimella ``round_raw``. Näin puukkokierros ei tuota tick- eikä
tapahtumarivejä, eikä adapterin tarvitse tuntea numerointisääntöä.

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
epäonnistumisella.** Jos parsinta kaatuu ja arkistossa on jo ehjät taulut ja
niiden ``ok``-manifesti, kaikki jätetään koskematta. Muussa tapauksessa
kirjoitetaan ``parse_failed``-manifesti, joka estää ohituksen ja kertoo syyn.
Vajaata taulua ei synny kummassakaan tapauksessa, koska kirjoitukset ovat
atomisia ja tapahtuvat vasta kaikkien tarkistusten jälkeen. Manifesti
kirjoitetaan viimeisenä, joten keskeytynyt ajo näkyy seuraavalla kerralla
puuttuvana tuloksena eikä ajantasaisena.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

import polars as pl

from pappascout.adapters.protocols import (
    EVENTS_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoParser,
    DemoTables,
)
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
from pappascout.domain.sampling import FIRST_CONTACT_SAMPLE
from pappascout.domain.schemas import EVENTS, ROUNDS, TICKS, validate
from pappascout.domain.utility import DETONATE, THROWN
from pappascout.errors import DemoUnavailable, ParseError, SchemaError
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "TABLE",
    "TICKS_TABLE",
    "EVENTS_TABLE",
    "TOOLS",
    "run",
    "resolve_demo",
    "map_demo_id_from_path",
    "default_parser",
]

STAGE = "parse"
TABLE = "rounds"
TICKS_TABLE = "ticks"
EVENTS_TABLE = "events"

#: Työkalut, joiden versio muuttaa tämän vaiheen tuloksen (manifest-moduulin
#: sääntö). Pappascoutin omaa versiota ei merkitä: korjauspäivitys ei saa
#: pakottaa koko arkiston uudelleenparsintaa.
TOOLS = ("demoparser2",)

#: Virheet, joista kirjataan yksikkökohtainen tila manifestiin (AD-9).
_RECORDED_ERRORS = (ParseError, SchemaError, OSError, pl.exceptions.PolarsError)


def default_parser(settings: ParseSettings) -> DemoParser:
    """Tuotannon demoparser2-toteutus.

    Tuonti on funktion sisällä, jotta tämän moduulin tuominen ei lataa
    demoparser2:ta -- vaihe itse tuntee vain portin, ja testit antavat sille
    feikin.

    Args:
        settings: ``[parse]``-osio. Ensikontaktin säännön parametrit ovat
            asetuksia eivätkä koodia, joten adapteri saa ne kutsussa eikä lue
            niitä itse.
    """
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    return Demoparser2Adapter(
        exclude_weapons=settings.first_contact_exclude_weapons,
        fallback_death=settings.first_contact_fallback_death,
        area_snap_units=settings.area_snap_units,
    )


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
    found = archive.find_demo(map_demo_id)
    if found is not None:
        return map_demo_id, found

    import_dir = archive.import_dir()
    for suffix in DEMO_SUFFIXES:
        path = import_dir / f"{map_demo_id}{suffix}"
        if path.is_file():
            return map_demo_id, path

    searched = [str(archive.demo(map_demo_id, s)) for s in DEMO_SUFFIXES]
    searched += [str(import_dir / f"{map_demo_id}{s}") for s in DEMO_SUFFIXES]
    listing = "\n".join(f"    {p}" for p in searched)
    raise DemoUnavailable(
        f"Demoa {map_demo_id} ei löytynyt.\n"
        "Etsin näistä poluista:\n"
        f"{listing}\n"
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


def _check_port_columns(
    df: pl.DataFrame,
    expected_columns: tuple[str, ...],
    table_name: str,
    contract: str,
) -> None:
    """Tarkista adapterin tuottama taulu ennen jatkokäsittelyä.

    Vaatimus on **täsmällinen sarakejoukko**, ei osajoukko: ylimääräinen sarake
    tarkoittaa, että portin toteutus ja sopimus ovat erkaantuneet, ja se
    päätyisi hiljaa mukaan tai kaatuisi vasta domain-kerroksessa.
    """
    received = set(df.columns)
    expected = set(expected_columns)
    missing = sorted(expected - received)
    extra = sorted(received - expected)
    if not missing and not extra:
        return
    parts = []
    if missing:
        parts.append(f"puuttuu: {', '.join(missing)}")
    if extra:
        parts.append(f"ylimääräisiä: {', '.join(extra)}")
    raise SchemaError(
        f"Demoportti palautti sopimuksen vastaisen {table_name} -- "
        f"{'; '.join(parts)}.\n"
        f"Portin sopimus on adapters/protocols.py:n {contract}."
    )


def _check_two_rows_per_round(df: pl.DataFrame) -> None:
    """Jokaisella kierroksella on oltava tasan kaksi riviä.

    Taulu on pitkä: yksi rivi kummallekin joukkueelle. Kolmas rivi tai
    puuttuva pari läpäisisi skeeman mutta kaksinkertaistaisi tai puolittaisi
    joukkuekohtaiset summat kaikessa myöhemmässä analyysissa.
    """
    round_count = df["round_no"].n_unique()
    if df.height == 2 * round_count:
        return
    deviating = (
        df.group_by("round_no")
        .len()
        .filter(pl.col("len") != 2)
        .sort("round_no")
        .head(5)
    )
    raise SchemaError(
        f"Kierrostaulussa on {df.height} riviä {round_count} kierrokselle; "
        "pitäisi olla tasan kaksi riviä per kierros (yksi kummallekin "
        "joukkueelle).\n"
        f"Poikkeavat kierrokset: {deviating.to_dicts()}"
    )


def _round_stats(df: pl.DataFrame, skipped_rounds: int = 0) -> dict[str, object]:
    """Kierrostaulun luvut."""
    if df.is_empty():
        return {
            "rounds": 0,
            "rows": 0,
            "max_round_no": 0,
            "skipped_rounds": skipped_rounds,
            "no_freeze_end": 0,
        }
    return {
        "rounds": int(df["round_no"].n_unique()),
        "rows": int(df.height),
        "max_round_no": int(df["round_no"].max() or 0),
        "skipped_rounds": skipped_rounds,
        "no_freeze_end": int(
            df.filter(pl.col("status") == "no_freeze_end")["round_no"].n_unique()
        ),
    }


def _stats(
    df: pl.DataFrame,
    ticks: pl.DataFrame,
    events: pl.DataFrame,
    skipped_rounds: int = 0,
) -> dict[str, object]:
    """Käyttäjälle näytettävät luvut valmiista tauluista."""
    stats = _round_stats(df, skipped_rounds)
    stats.update(_tick_stats(ticks))
    stats.update(_event_stats(events))
    return stats


def _tick_stats(ticks: pl.DataFrame) -> dict[str, object]:
    """Näytepisteiden luvut.

    ``sample_points`` on kierros x hetki, ei rivimäärä: kymmenen pelaajan rivit
    ovat sama näytepiste. ``first_contact_rounds`` kertoo, monellako
    kierroksella kontakti ylipäätään löytyi -- pelkkä rivimäärä ei paljastaisi,
    jos sääntö jäisi puremattomaksi.
    """
    if ticks.is_empty():
        return {
            "tick_rows": 0,
            "sample_points": 0,
            "sample_rounds": 0,
            "first_contact_rounds": 0,
        }
    contact = ticks.filter(pl.col("sample_kind") == FIRST_CONTACT_SAMPLE)
    return {
        "tick_rows": int(ticks.height),
        "sample_points": int(
            ticks.select("round_no", "sample_kind", "sample_t_s").n_unique()
        ),
        "sample_rounds": int(ticks["round_no"].n_unique()),
        "first_contact_rounds": int(contact["round_no"].n_unique()),
    }


def _event_stats(events: pl.DataFrame) -> dict[str, object]:
    """Utility-tapahtumien luvut.

    Heitot ja räjähdykset erikseen, koska niiden **erotus** on itsessään
    havainto: räjähtämättömiä kranaatteja on aina muutama, mutta iso ero
    tarkoittaisi, että radan loppu jää tunnistamatta.

    Alueesta kerrotaan kolme lukua, koska ne ovat kolmea eri laatua olevaa
    tietoa eivätkä saa niputtua yhdeksi:

    ``utility_area_observed``
        Heittäjän oma alue. Havainto.
    ``utility_area_snapped``
        Räjähdyksen alue lähimmältä elossa olevalta pelaajalta. Arvio, jonka
        luotettavuuden ``snap_distance`` kertoo rivikohtaisesti.
    ``utility_area_unnamed``
        Napsautus osui, mutta lähimmällä pelaajalla ei ole aluenimeä. Sekin on
        havainto -- pelin nimeämätön alue -- eikä sama asia kuin "kukaan ei
        ollut lähellä".

    ``utility_without_area`` on näiden ulkopuolelle jäävä loppu: alue puuttuu
    kokonaan.
    """
    if events.is_empty():
        return {
            "event_rows": 0,
            "utility_throws": 0,
            "utility_detonations": 0,
            "utility_rounds": 0,
            "utility_area_observed": 0,
            "utility_area_snapped": 0,
            "utility_area_unnamed": 0,
            "utility_without_area": 0,
        }
    kinds = events["event_kind"]
    sources = events["area_source"]
    return {
        "event_rows": int(events.height),
        "utility_throws": int((kinds == THROWN).sum()),
        "utility_detonations": int((kinds == DETONATE).sum()),
        "utility_rounds": int(events["round_no"].n_unique()),
        "utility_area_observed": int((sources == "observed").sum()),
        "utility_area_snapped": int((sources == "snapped").sum()),
        "utility_area_unnamed": int(
            events.filter(
                pl.col("area").is_null() & pl.col("snap_distance").is_not_null()
            ).height
        ),
        "utility_without_area": int(events["area"].null_count()),
    }


def _read_table(path: Path) -> pl.DataFrame | str:
    """Lue taulu tai palauta virheen kuvaus merkkijonona."""
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        return f"{type(exc).__name__}: {exc}"


def _existing_stats(
    table_abs: Path, ticks_abs: Path, events_abs: Path
) -> dict[str, object]:
    """Luvut ohitettuun ajoon: luetaan valmiit taulut, ei parsita demoa.

    Taulut luetaan **erikseen**. Yhteinen try-lohko hukkaisi kierrosluvut
    silloin, kun vain näytepistetaulu on lukukelvoton, ja käyttäjä näkisi
    "lukuja ei saatu" myös siitä mikä oli aivan ehjä.

    Jos tulosta ei saada luettua, palautetaan tieto siitä eikä nollia --
    nollarivi näyttäisi siltä, että demossa ei ollut yhtään kierrosta.
    """
    rounds = _read_table(table_abs)
    ticks = _read_table(ticks_abs)
    events = _read_table(events_abs)

    # Kierros- ja näytepistetaulu ovat vaiheen ydintulos. Jos **kumpikaan** ei
    # aukea, koko tulos on lukukelvoton eikä siitä koota osittaista
    # yhteenvetoa: pelkkien utility-lukujen näyttäminen antaisi vaikutelman
    # ajantasaisesta tuloksesta.
    if isinstance(rounds, str) and isinstance(ticks, str):
        return {"unreadable": rounds}

    stats: dict[str, object] = {}
    if isinstance(rounds, str):
        stats["unreadable"] = rounds
    else:
        stats.update(_round_stats(rounds))
    if isinstance(ticks, str):
        stats["ticks_unreadable"] = ticks
    else:
        stats.update(_tick_stats(ticks))
    if isinstance(events, str):
        stats["events_unreadable"] = events
    else:
        stats.update(_event_stats(events))
    return stats


def run(
    settings: ParseSettings,
    archive: ArchivePaths,
    map_demo_id: str,
    parser: DemoParser,
    *,
    demo_path: Path | None = None,
    force: bool = False,
) -> StageResult:
    """Parsi yksi demo kierros-, näytepiste- ja tapahtumatauluiksi.

    Args:
        settings: ``[parse]``-osio -- ainoa osio, jonka tämä vaihe näkee.
        archive: Arkiston polut.
        map_demo_id: Yksikön tunniste, ``{match_id}-{map_index}``.
        parser: Demoportti (AD-8). Tuotannossa :func:`default_parser`.
        demo_path: Demotiedosto. Oletuksena etsitään arkistosta.
        force: Ohita manifestin täsmäys ja parsi joka tapauksessa.

    Returns:
        :class:`~pappascout.stages.StageResult`, jossa ``stats`` kertoo
        kierrosten määrän, rivimäärän, suurimman kierrosnumeron, ohitettujen ja
        ankkurittomien kierrosten lukumäärän, näytepisteiden ja
        ensikontaktien määrän sekä utility-heittojen, räjähdysten ja
        aluettomien tapahtumien määrän.

    Raises:
        DemoUnavailable: Jos demoa ei löydy tai sitä ei voi lukea.
        ~pappascout.errors.ParseError: Jos demo ei ole luettavissa tai sen
            sisältö rikkoo CS2:n sääntöjä.
        ~pappascout.errors.SchemaError: Jos tulos ei vastaa sopimusta.
    """
    started = time.perf_counter()
    map_demo_id = safe_component(map_demo_id, "map_demo_id")

    if demo_path is None:
        _, demo_path = resolve_demo(archive, map_demo_id)
    demo_path = Path(demo_path)

    table_rel = parsed_table(map_demo_id, TABLE)
    ticks_rel = parsed_table(map_demo_id, TICKS_TABLE)
    events_rel = parsed_table(map_demo_id, EVENTS_TABLE)
    manifest_rel = parsed_manifest(map_demo_id)
    table_abs = archive.resolve(table_rel)
    ticks_abs = archive.resolve(ticks_rel)
    events_abs = archive.resolve(events_rel)
    manifest_abs = archive.resolve(manifest_rel)

    result_id = str(PurePosixPath("parsed") / map_demo_id)
    inputs = [
        ManifestInput(
            result_id=f"demo/{map_demo_id}",
            sha256=_demo_fingerprint(archive, map_demo_id, demo_path),
        )
    ]
    params_hash = _params_hash(settings)
    versions = tool_versions(*TOOLS)

    # Tulokset, jotka **tämän version** ajo tuottaa. Manifestin oma
    # outputs-lista ei riita ohituksen ehdoksi: vanha manifesti nimeaa vain ne
    # taulut, jotka silloinen koodi osasi kirjoittaa, joten uusi taulu ei
    # koskaan syntyisi arkistoon jo parsituille demoille.
    expected_outputs = (
        (table_rel, table_abs),
        (ticks_rel, ticks_abs),
        (events_rel, events_abs),
    )

    existing = Manifest.read_if_exists(manifest_abs)
    if (
        not force
        and existing is not None
        and existing.is_current(
            inputs=inputs,
            params_hash=params_hash,
            tool_versions=versions,
            root=archive.root,
        )
        and all(path.is_file() for _, path in expected_outputs)
    ):
        return StageResult(
            stage=STAGE,
            unit=map_demo_id,
            status="ok",
            skipped=True,
            outputs=tuple(PurePosixPath(o) for o in existing.outputs),
            manifest_path=manifest_rel,
            reason=(
                "Tulos on ajan tasalla: manifesti täsmää eikä demoa tarvitse "
                "parsia uudelleen."
            ),
            duration_s=time.perf_counter() - started,
            stats=_existing_stats(table_abs, ticks_abs, events_abs),
        )

    try:
        df, ticks, events, skipped_rounds, unnumbered = _parse_tables(
            parser, settings, demo_path, map_demo_id
        )
        # Kirjoitus on saman virhekäsittelyn sisällä kuin parsinta: levy voi
        # täyttyä tai OneDrive lukita tiedoston, ja silloinkin manifestiin on
        # jäätävä merkintä epäonnistumisesta -- muuten seuraava ajo ohittaisi
        # vaiheen puolikkaan tuloksen päältä.
        _write_tables(
            ((table_abs, df), (ticks_abs, ticks), (events_abs, events))
        )
    except _RECORDED_ERRORS as exc:
        _record_failure(
            archive=archive,
            manifest_abs=manifest_abs,
            tables_abs=(table_abs, ticks_abs, events_abs),
            existing=existing,
            result_id=result_id,
            params_hash=params_hash,
            inputs=inputs,
            versions=versions,
            reason=str(exc),
        )
        raise

    # Manifesti viimeisenä: keskeytynyt ajo näkyy seuraavalla kerralla
    # puuttuvana tuloksena eikä ajantasaisena.
    Manifest.new(
        result_id=result_id,
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=versions,
        status="ok",
        outputs=(str(table_rel), str(ticks_rel), str(events_rel)),
    ).write(manifest_abs)

    stats = _stats(df, ticks, events, skipped_rounds)
    # Vain tuoreesta ajosta: numeroimattomien kierrosten rivit eivät ole
    # taulussa, joten ohitetusta ajosta lukua ei voi lukea takaisin.
    stats["utility_unnumbered_rounds"] = unnumbered
    diagnostics = getattr(parser, "diagnostics", None)
    if diagnostics is not None:
        stats["tick_rate"] = diagnostics.tick_rate
        stats["tick_rate_measured"] = diagnostics.tick_rate_measured
        # Näitä ei voi laskea valmiista taulusta: vajaa näytepiste, ohitettu
        # vahinkotapahtuma ja pudotettu kranaatti näkyvät vain siinä hetkessä,
        # kun demoa luetaan. Ohitetussa ajossa ne siis puuttuvat, ja se on
        # oikein.
        stats["partial_samples"] = getattr(diagnostics, "partial_samples", 0)
        stats["unknown_side_events"] = getattr(
            diagnostics, "unknown_side_events", 0
        )
        stats["grenades_without_thrower"] = getattr(
            diagnostics, "grenades_without_thrower", 0
        )
        stats["grenades_outside_rounds"] = getattr(
            diagnostics, "grenades_outside_rounds", 0
        )
        for name in (
            "grenades_unknown_side",
            "grenades_unknown_type",
            "grenades_fire_type_unresolved",
            "grenades_detonating_after_round",
            "grenade_ticks_without_players",
            "grenades_id_reused_in_round",
        ):
            stats[name] = getattr(diagnostics, name, 0)

    return StageResult(
        stage=STAGE,
        unit=map_demo_id,
        status="ok",
        skipped=False,
        outputs=(table_rel, ticks_rel, events_rel),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - started,
        stats=stats,
    )


def _write_tables(tables: tuple[tuple[Path, pl.DataFrame], ...]) -> None:
    """Kirjoita kaikki taulut yhtenä tapahtumana.

    Jokainen taulu menee ensin omaan väliaikaistiedostoonsa, ja vasta kun
    **kaikki** on kirjoitettu, ne siirretään kohteisiinsa. Peräkkäiset
    ``atomic_path``-lohkot eivät riitä: jos toinen kirjoitus kaatuu, ensimmäinen
    olisi jo paikallaan ja arkistoon jäisi kierrostaulu, joka kertoo 21
    kierrosta, sekä näytepistetaulu, joka tuntee niistä kaksi -- yhdistelmä,
    joka läpäisisi jokaisen skeematarkistuksen.

    Siirto itse ei ole yksi atominen operaatio (yksi ``os.replace`` taulua
    kohden), mutta niiden väliin jäävä ikkuna on mikrosekunteja eikä sisällä
    I/O:ta.
    """
    with ExitStack() as stack:
        pending_writes = [
            (stack.enter_context(atomic_path(target)), frame)
            for target, frame in tables
        ]
        for tmp, frame in pending_writes:
            frame.write_parquet(tmp)
    # ExitStack purkaa lohkot vasta tässä, ja jokainen atomic_path tekee oman
    # renamensa. Poikkeus missä tahansa kirjoituksessa siivoaa kaikki
    # väliaikaistiedostot eikä koske kohteisiin.


def _parse_tables(
    parser: DemoParser,
    settings: ParseSettings,
    demo_path: Path,
    map_demo_id: str,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, int, int]:
    """Lue demo ja rakenna valmiit, tarkistetut taulut.

    Returns:
        ``(kierrostaulu, näytepistetaulu, tapahtumataulu, ohitettujen
        kierrosten määrä, numeroimattomilta kierroksilta pudonneiden
        utility-heittojen määrä)``.
    """
    tables: DemoTables = parser.parse_demo(demo_path, settings.snapshot_seconds)
    _check_port_columns(
        tables.rounds, ROUNDS_ADAPTER_COLUMNS, "kierrostaulun", "ROUNDS_ADAPTER_COLUMNS"
    )
    _check_port_columns(
        tables.ticks, TICKS_ADAPTER_COLUMNS, "näytepistetaulun", "TICKS_ADAPTER_COLUMNS"
    )
    _check_port_columns(
        tables.events,
        EVENTS_ADAPTER_COLUMNS,
        "tapahtumataulun",
        "EVENTS_ADAPTER_COLUMNS",
    )

    numbered = mark_played_rounds(tables.rounds)
    skipped_rounds = int(
        numbered.filter(pl.col("round_no").is_null())["round_raw"].n_unique()
    )
    played = numbered.filter(pl.col("round_no").is_not_null())

    df = played.select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in ROUNDS if name != "map_demo_id"],
    ).sort("round_no", "side")

    if df.is_empty():
        raise ParseError(
            f"Demosta {demo_path.name} ei löytynyt yhtään pelattua kierrosta "
            f"({skipped_rounds} kierrosrajaa oli warmupia, puukkokierros tai "
            "uudelleenkäynnistys).\n"
            "Tyhjää tulosta ei kirjoiteta -- muuten se jäisi manifestin "
            "perusteella pysyvästi ohitetuksi. Tarkista, että demo on koko "
            "ottelun tallenne."
        )

    validate(df, ROUNDS, TABLE)
    check_win_reasons(df)
    _check_two_rows_per_round(df)

    ticks = _number_ticks(tables.ticks, numbered, map_demo_id)
    validate(ticks, TICKS, TICKS_TABLE)

    if ticks.is_empty():
        raise ParseError(
            f"Demosta {demo_path.name} syntyi {df['round_no'].n_unique()} "
            "pelattua kierrosta mutta ei yhtään näytepistettä.\n"
            "Tyhjää asetelmataulua ei kirjoiteta ok-tuloksena: se jäisi "
            "manifestin perusteella pysyvästi ohitetuksi ja aggregointi "
            "raportoisi kartan ilman yhtään asetelmaa. Tarkista "
            "[parse].snapshot_seconds ja se, löytyykö kierroksilta "
            "freezetime-ankkuri."
        )

    # Tyhjää tapahtumataulua **ei** kohdella virheenä, toisin kuin kahta muuta:
    # demossa on aina pelattuja kierroksia ja asetelmia, mutta utility voi
    # aidosti puuttua (harjoitusottelu, pelkkiä pistoolikierroksia). Virhe
    # estäisi koko demon parsinnan tiedosta, joka on itsessään havainto.
    events, unnumbered = _number_events(tables.events, numbered, map_demo_id)
    validate(events, EVENTS, EVENTS_TABLE)

    return df, ticks, events, skipped_rounds, unnumbered


def _number_ticks(
    ticks: pl.DataFrame, numbered: pl.DataFrame, map_demo_id: str
) -> pl.DataFrame:
    """Liitä näytepisteisiin ``round_no`` ja pudota numeroimattomat kierrokset.

    Adapteri näytteistää kaikki ankkuroidut kierrosrajat, koska se ei tunne
    numerointisääntöä -- sen omistaa
    :func:`~pappascout.domain.rounds.mark_played_rounds`. Warmupin ja
    puukkokierroksen rivit poistuvat siis vasta tässä, samalla päätöksellä kuin
    kierrostaulusta, jolloin taulut eivät voi olla eri mieltä siitä mikä
    kierros pelattiin.
    """
    numbers = (
        numbered.select("round_raw", "round_no")
        .unique(subset=["round_raw"], keep="first")
        .filter(pl.col("round_no").is_not_null())
    )
    joined = (
        ticks.drop("round_no")
        .join(numbers, on="round_raw", how="inner")
        .select(
            pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
            *[pl.col(name) for name in TICKS if name != "map_demo_id"],
        )
        .sort("round_no", "sample_t_s", "sample_kind", "side", "player_id")
    )
    return joined


def _number_events(
    events: pl.DataFrame, numbered: pl.DataFrame, map_demo_id: str
) -> tuple[pl.DataFrame, int]:
    """Liitä tapahtumiin ``round_no`` ja pudota numeroimattomat kierrokset.

    Sama päätös ja sama liitos kuin näytepisteillä (:func:`_number_ticks`):
    lämmittelyssä ja puukkokierroksella heitetty utility poistuu tässä, jolloin
    taulut eivät voi olla eri mieltä siitä mikä kierros pelattiin.

    Lajitteluavain on ``(round_no, grenade_entity_id, event_kind, t_s)``.
    ``event_kind`` on ennen ``t_s``:ää, jotta saman kranaatin heitto tulee aina
    ennen sen räjähdystä silloinkin, kun molemmilla on sama ``t_s``; se on
    Enum, joten järjestys on luettelon järjestys eikä aakkosjärjestys.

    Returns:
        ``(taulu, pudonneet heitot)``. Jälkimmäinen lasketaan, koska kolme
        muuta utilityn pudotussyytä raportoidaan nimenomaan siksi, ettei
        utility katoa hiljaa -- eikä tämä saa olla poikkeus.
    """
    numbers = (
        numbered.select("round_raw", "round_no")
        .unique(subset=["round_raw"], keep="first")
        .filter(pl.col("round_no").is_not_null())
    )
    joined = (
        events.drop("round_no")
        .join(numbers, on="round_raw", how="inner")
        .select(
            pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
            *[pl.col(name) for name in EVENTS if name != "map_demo_id"],
        )
        .sort("round_no", "grenade_entity_id", "event_kind", "t_s")
    )
    before = int(events.filter(pl.col("event_kind") == THROWN).height)
    after = int(joined.filter(pl.col("event_kind") == THROWN).height)
    return joined, before - after


def _record_failure(
    *,
    archive: ArchivePaths,
    manifest_abs: Path,
    tables_abs: tuple[Path, ...],
    existing: Manifest | None,
    result_id: str,
    params_hash: str,
    inputs: list[ManifestInput],
    versions: dict[str, str],
    reason: str,
) -> None:
    """Kirjaa epäonnistuminen manifestiin -- mutta älä tuhoa ehjää tulosta.

    Jos arkistossa on jo kelvolliset taulut ja niiden ``ok``-manifesti, kaikki
    jätetään paikalleen. Muuten manifestiin kirjataan ``parse_failed``, joka
    sekä kertoo syyn että estää ohituksen seuraavalla ajolla.

    **Kaikkien** taulujen on oltava paikallaan: vanha ``rounds`` uuden
    ``ticks``-taulun kanssa olisi puolikas tulos, joka näyttäisi ehjältä.
    """
    intact_result = (
        existing is not None
        and existing.status == "ok"
        and existing.outputs_present(archive.root)
        and all(path.is_file() for path in tables_abs)
    )
    if intact_result:
        return
    Manifest.new(
        result_id=result_id,
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=versions,
        status="parse_failed",
        reason=reason,
        outputs=(),
    ).write(manifest_abs)

