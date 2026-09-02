"""``render`` -- putken viimeinen vaihe: ``report.json`` Markdowniksi.

Vaihe lukee **yhden tiedoston** (``aggregates/<team_key>/report.json``) ja
kirjoittaa **yhden tiedoston** (``reports/<team_key>/<aikaleima>-<slug>.md``).
Se ei lue demoa, ei tauluja eikä luokittelua, eikä se laske mitään: jokainen
raportissa esiintyvä luku on ``report.json``issa valmiina. Jos raportti
tarvitsee luvun, jota siellä ei ole, korjaus tehdään ``aggregate``-vaiheeseen
(Story 2.3) -- ei tänne.

Uusi tiedosto joka ajolla, ei koskaan ylikirjoitusta
----------------------------------------------------
``report.json`` ylikirjoitetaan aina, raportti ei koskaan. Nimessä on
aikaleima minuutin tarkkuudella, ja saman minuutin sisällä ajetut saavat
nollatäytetyn päätteen ``-02``, ``-03``. Nimi **varataan atomisesti**
(``O_CREAT | O_EXCL``) ennen kirjoitusta: pelkkä ``exists()``-tarkistus
jättäisi raon, jossa kaksi rinnakkaista ajoa valitsisi saman nimen ja
jälkimmäinen tuhoaisi ensimmäisen. Arkisto on OneDrivessa ja kahden koneen
yhteinen, joten rako ei ole teoreettinen.

**Varaus maksaa myös jotain, ja se on syytä sanoa ääneen.** Varattu tiedosto on
olemassa ennen kuin sisältö on, joten (a) epäonnistunut kirjoitus jättäisi
nollatavuisen ``.md``:n, joka näyttäisi raportilta -- siksi varaus perutaan
virheen sattuessa -- ja (b) OneDrive voi ehtiä avata varatun tiedoston, jolloin
``os.replace`` samaan nimeen kaatuu ``PermissionError``iin. Jälkimmäinen on
harvinainen mutta mahdollinen, ja se käännetään suomenkieliseksi virheeksi
pinojäljen sijaan. Kumpikaan ei ole syy luopua varauksesta: hiljainen
ylikirjoitus olisi pahempi kuin näkyvä virhe.

Vaihetta ei ohiteta
-------------------
Muut vaiheet ohittavat itsensä, kun manifesti täsmää. ``render`` ei: käyttäjä
ajaa ``report``-komennon silloin kun hän haluaa raportin, ja ohitus jättäisi
hänet ilman tiedostoa jonka hän pyysi. Manifesti kirjoitetaan silti, ja se on
**raporttikohtainen**: yhteinen manifesti kestäisi huonosti juuri sitä
rinnakkaisuutta, jonka varalta nimi varataan.

Skeemaversio tarkistetaan ennen mallia
--------------------------------------
Vanhalla versiolla kirjoitettu ``report.json`` voi validoitua nykyistä mallia
vasten kenttä kentältä ja tarkoittaa silti eri asiaa. Versio luetaan siksi
raa'asta JSONista **ennen** pydantic-validointia: muuten käyttäjä saisi
kenttäkohtaisen validointivirheen sen sijaan että hänelle kerrottaisiin
ajamaan ``aggregate`` uudelleen.

Manifesti ja parametrihash
--------------------------
Syöte on joukkueen ``aggregate``-manifesti tunnisteineen. Parametrihash
lasketaan **raporttimallin sisällöstä ja vaiheen omasta asetusosiosta**:
mallin muokkaaminen muuttaa raportin sisältöä, ja niin muuttaa
karsintasäännön säätäminenkin. Ilman kumpaakaan manifesti väittäisi kahta
eri raporttia samaksi tulokseksi.

Asetusosio tuli hashiin Story 2.13:ssa, jossa vaihe sai ensimmäiset omat
asetuksensa (``[report]``, karsintasäännöt). Sitä ennen hash oli pelkkä
mallin tiiviste, koska vaihe ei lukenut yhtäkään asetusta -- kynnykset
tulevat ``report.json``ista. **Asetusta ei voi lisätä vaiheeseen, joka ei
huomaa sen muuttumista**: se on Story 1.8:n vika, joka on tässä projektissa
löytynyt kolmesti, ja siksi hash korjattiin samassa tarinassa kuin asetukset
lisättiin.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pappascout import __version__
from pappascout.archive.atomic_write import atomic_write_text
from pappascout.archive.manifest import (
    Manifest,
    ManifestInput,
    compute_params_hash,
    tool_versions,
)
from pappascout.archive.paths import (
    MAX_REPORTS_PER_MINUTE,
    REPORT_TIMESTAMP_FORMAT,
    ArchivePaths,
    classified_round_list,
    render_manifest,
    report_json,
    report_manifest,
    report_markdown,
    report_name,
    safe_component,
)
from pappascout.domain.aggregate import team_slug
from pappascout.domain.models import ReportSettings
from pappascout.domain.report import REPORT_SCHEMA_VERSION, Report
from pappascout.errors import PappascoutError
from pappascout.render import render_report, round_list_demo_ids, template_digest
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "TOOLS",
    "run",
    "team_keys",
    "resolve_team",
    "read_report",
    "reserve_path",
    "round_list_paths",
]

STAGE = "render"

#: Jinja2 muuttaa raportin muotoa versioiden välillä (esimerkiksi
#: tyhjätilasäännöt), joten sen versio kuuluu manifestiin -- toisin kuin
#: ``aggregate``issa, jossa mikään kirjasto ei vaikuta tulokseen.
TOOLS: tuple[str, ...] = ("jinja2",)


def _tools() -> dict[str, str]:
    """Työkaluversiot manifestiin."""
    return tool_versions(*TOOLS)


def run(
    settings: ReportSettings,
    archive: ArchivePaths,
    team: str | None,
    *,
    now: datetime | None = None,
) -> StageResult:
    """Kirjoita joukkueen ``report.json``ista yksi Markdown-raportti.

    Args:
        settings: ``[report]``-osio eli karsintasäännöt (Story 2.13). Vaihe
            saa **vain oman osansa** (AD-3), ja sama osio menee sekä
            renderöintiin että parametrihashiin -- luettu asetus, joka ei ole
            hashissa, väittäisi manifestissa kahta eri raporttia samaksi.
        archive: Arkiston polut.
        team: Joukkueen tunniste tai sen yksikäsitteinen alkuosa. ``None``
            tuottaa suomenkielisen virheen, joka listaa aggregoidut joukkueet.
        now: Aikaleima tiedostonimeen, **paikallista aikaa**. Testien takia
            annettavissa; oletuksena kellonaika.

    Returns:
        :class:`~pappascout.stages.StageResult`, jonka ``outputs`` sisältää
        kirjoitetun raportin ja ``stats`` sen luvut.

    Raises:
        ~pappascout.errors.PappascoutError: Jos joukkuetta ei tunnisteta, jos
            ``report.json`` puuttuu tai on eri skeemaversiota, jos vapaata
            tiedostonimeä ei löydy tai jos kirjoitus epäonnistuu.
    """
    started = time.perf_counter()
    team_key = resolve_team(archive, team)

    json_rel = report_json(team_key)
    report = read_report(archive.resolve(json_rel), team_key)
    markdown = render_report(
        report,
        settings=settings,
        round_list_paths=round_list_paths(archive, report),
    )

    stamp = (now or datetime.now()).strftime(REPORT_TIMESTAMP_FORMAT)
    slug = team_slug(report.team.slug or team_key)
    path, name = reserve_path(archive, team_key, stamp, slug)
    markdown_rel = report_markdown(team_key, name)

    try:
        atomic_write_text(path, markdown)
    except OSError as exc:
        # Varaus on tyhjä tiedosto, joka on jo hakemistossa. Jos se jää sinne,
        # se näyttää raportilta, vie järjestysluvun eikä erotu mistään -- se ei
        # ole edes atomisen kirjoituksen väliaikaistiedosto, joten siivousta
        # etsivä tarkistus ei löydä sitä.
        with contextlib.suppress(OSError):
            path.unlink()
        raise PappascoutError(
            f"Raporttia ei voitu kirjoittaa polkuun {path}: {exc}\n"
            "Varaus peruttiin, joten hakemistoon ei jäänyt tyhjää raporttia. "
            "Tarkista levytila ja se, ettei OneDrive pidä tiedostoa auki, ja "
            "aja komento uudelleen."
        ) from exc

    manifest_rel = render_manifest(team_key, name)
    Manifest.new(
        result_id=str(markdown_rel),
        stage=STAGE,
        params_hash=_params_hash(settings),
        inputs=_inputs(archive, team_key),
        tool_versions=_tools(),
        status="ok",
        outputs=(str(markdown_rel),),
    ).write(archive.resolve(manifest_rel))

    return StageResult(
        stage=STAGE,
        unit=team_key,
        status="ok",
        skipped=False,
        outputs=(markdown_rel,),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - started,
        stats=_stats(report, markdown),
    )


# -- Joukkueen valinta -----------------------------------------------------------


def team_keys(archive: ArchivePaths) -> list[str]:
    """Joukkueet, joilla on aggregoitu ``report.json``.

    Luettelo luetaan ``aggregates/``-hakemistosta eikä ``classified/``:sta:
    ``render``in syöte on aggregointi, ja luokiteltu mutta aggregoimaton
    joukkue tarjoutuisi valittavaksi vain kaatuakseen heti perään.
    """
    root = archive.resolve(PurePosixPath("aggregates"))
    if not root.is_dir():
        return []
    return sorted(
        directory.name
        for directory in root.iterdir()
        if directory.is_dir() and (directory / "report.json").is_file()
    )


def resolve_team(archive: ArchivePaths, team: str | None) -> str:
    """Tulkitse ``--team`` aggregoiduksi joukkueeksi.

    Hyväksyy sekä täyden tunnisteen että sen yksikäsitteisen alkuosan -- 16
    merkin tiiviste on epämukava kirjoittaa käsin.

    **Tyhjä merkkijono ei ole alkuosa.** Jokainen tunniste alkaa tyhjällä
    merkkijonolla, joten ``--team ""`` täsmäisi kaikkiin ja valitsisi hiljaa
    ainoan -- eli tekisi juuri sen, minkä ``--team``in vaatiminen on tarkoitus
    estää. Sama koskee pelkkiä välilyöntejä.

    Raises:
        PappascoutError: Jos tunniste puuttuu, on tyhjä, ei täsmää tai täsmää
            useampaan. Viesti listaa aina vaihtoehdot, joten seuraava komento
            on suoraan kopioitavissa.
    """
    available = team_keys(archive)
    if not available:
        raise PappascoutError(
            "Arkistossa ei ole yhtään aggregoitua joukkuetta, joten "
            "raportoitavaa ei ole.\n"
            "Aja ensin: uv run pappascout aggregate --team <tunniste>"
        )
    if team is None or not team.strip():
        problem = (
            "Kerro --team-valinnalla, minkä joukkueen raportti kirjoitetaan."
            if team is None
            else "Joukkuetunniste on tyhjä; tyhjä alkuosa täsmäisi kaikkiin."
        )
        raise PappascoutError(f"{problem}\n{_team_listing(available)}")

    query = team.strip().lower()
    matches = [key for key in available if key.lower() == query]
    if not matches:
        matches = [key for key in available if key.lower().startswith(query)]
    if len(matches) == 1:
        return safe_component(matches[0], "team_key")

    problem = (
        f"Joukkuetunniste {team!r} täsmää useampaan kuin yhteen joukkueeseen."
        if matches
        else f"Joukkuetunniste {team!r} ei täsmää yhteenkään joukkueeseen."
    )
    raise PappascoutError(f"{problem}\n{_team_listing(available)}")


def _team_listing(available: list[str]) -> str:
    rows = "\n".join(f"    {key}" for key in available)
    return (
        "Arkiston aggregoidut joukkueet ovat:\n"
        + rows
        + "\nAnna tunniste kokonaan tai sen alkuosa, esimerkiksi:\n"
        + f"    --team {available[0][:8]}"
    )


# -- Syötteen luku ---------------------------------------------------------------


def read_report(path: Path, team_key: str) -> Report:
    """Lue ja tarkista ``report.json``.

    Tarkistuksia on neljä, ja jokainen tuottaa oman ohjeensa: puuttuva
    tiedosto, väärä skeemaversio, rikkinäinen sisältö ja **väärä joukkue**.
    Kolme ensimmäistä eivät ole sama virhe -- ensimmäisessä aggregointia ei
    ole ajettu, toisessa se on ajettu vanhalla versiolla, kolmannessa tiedosto
    on vioittunut.

    Neljäs on hienovaraisin: ``aggregate`` kirjoittaa ``team.key``:n samaksi
    kuin hakemiston nimi, joten ero tarkoittaa, että tiedosto on siirretty tai
    muokattu käsin. Ilman tarkistusta raportti nimettäisiin hakemiston mukaan
    mutta kierrosliitteen polut ja tilastot osoittaisivat toiseen joukkueeseen.

    Raises:
        PappascoutError: Kaikissa neljässä tapauksessa, viesti kertoo mitä
            tehdä seuraavaksi.
    """
    if not path.is_file():
        raise PappascoutError(
            f"Joukkueen {team_key} aggregointia ei löytynyt polusta {path}.\n"
            f"Aja ensin: uv run pappascout aggregate --team {team_key}"
        )
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PappascoutError(
            f"Tiedostoa {path} ei voitu lukea JSONina: {exc}\n"
            f"Aja aggregointi uudelleen: uv run pappascout aggregate --team "
            f"{team_key} --pakota"
        ) from exc

    version = raw.get("schema_version") if isinstance(raw, dict) else None
    if version != REPORT_SCHEMA_VERSION:
        raise PappascoutError(
            f"Raporttimallin skeemaversio ei täsmää: {path} on versiota "
            f"{version!r}, mutta tämä ohjelma tuntee version "
            f"{REPORT_SCHEMA_VERSION!r}.\n"
            "Raporttia ei kirjoiteta, koska vanha rakenne voi näyttää "
            "kelvolliselta ja tarkoittaa silti eri asiaa.\n"
            f"Aja aggregointi uudelleen: uv run pappascout aggregate --team "
            f"{team_key} --pakota"
        )

    try:
        report = Report.model_validate(raw)
    except (ValueError, PappascoutError) as exc:
        raise PappascoutError(
            f"Tiedosto {path} ei vastaa raporttimallia: {exc}\n"
            f"Aja aggregointi uudelleen: uv run pappascout aggregate --team "
            f"{team_key} --pakota"
        ) from exc

    if report.team.key != team_key:
        raise PappascoutError(
            f"Tiedosto {path} on hakemistossa {team_key}, mutta sen sisältö "
            f"koskee joukkuetta {report.team.key!r}.\n"
            "Raporttia ei kirjoiteta, koska se nimettäisiin hakemiston mukaan "
            "mutta kertoisi toisesta joukkueesta.\n"
            f"Aja aggregointi uudelleen: uv run pappascout aggregate --team "
            f"{report.team.key} --pakota"
        )
    return report


def round_list_paths(archive: ArchivePaths, report: Report) -> list[str]:
    """Kierroslistojen **absoluuttiset** polut kierrosliitettä varten.

    Polut rakennetaan täällä eikä näkymässä kahdesta syystä. Arkiston
    hakemistorakenne on ``archive.paths``in asia, eikä sitä saa kirjoittaa
    toiseen paikkaan käsin; ja raportti liitetään Discordiin, jossa lukijalla
    ei ole mitään tapaa tietää mihin arkiston juuri osoittaa -- suhteellinen
    polku olisi hänelle käyttökelvoton.
    """
    return [
        str(archive.resolve(classified_round_list(report.team.key, demo)))
        for demo in round_list_demo_ids(report)
    ]


# -- Tiedostonimen varaus --------------------------------------------------------


def reserve_path(
    archive: ArchivePaths, team_key: str, stamp: str, slug: str
) -> tuple[Path, str]:
    """Varaa vapaa raporttinimi atomisesti.

    Nimi luodaan tyhjänä tiedostona ``O_CREAT | O_EXCL``-lipuilla, jolloin
    varaus onnistuu täsmälleen yhdelle ajolle. Vasta sen jälkeen sisältö
    kirjoitetaan päälle atomisesti. Ilman varausta kaksi rinnakkaista ajoa
    voisi valita saman nimen ``exists()``-tarkistuksen ja kirjoituksen välissä.

    Vain ``FileExistsError`` tarkoittaa "nimi on varattu"; kaikki muu --
    kirjoituskelvoton hakemisto, levy täynnä, OneDriven lukko -- on virhe,
    joka on kerrottava suomeksi eikä päästettävä läpi raakana pinojälkenä.

    Returns:
        ``(absoluuttinen polku, tiedostonimi)``.

    Raises:
        PappascoutError: Jos hakemistoa ei voitu luoda, jos varaus epäonnistui
            muusta syystä kuin varatusta nimestä, tai jos vapaata nimeä ei
            löytynyt :data:`~pappascout.archive.paths.MAX_REPORTS_PER_MINUTE`
            yrityksellä.
    """
    directory = archive.reports_dir(team_key)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PappascoutError(
            f"Raporttihakemistoa {directory} ei voitu luoda: {exc}\n"
            "Tarkista arkiston polku ja kirjoitusoikeudet."
        ) from exc

    for ordinal in range(1, MAX_REPORTS_PER_MINUTE + 1):
        name = report_name(stamp, slug, ordinal)
        path = archive.report_markdown(team_key, name)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        except OSError as exc:
            raise PappascoutError(
                f"Raporttitiedostoa {path} ei voitu varata: {exc}\n"
                "Tarkista arkiston polku ja kirjoitusoikeudet."
            ) from exc
        os.close(handle)
        return path, name

    raise PappascoutError(
        f"Hakemistossa {directory} on jo {MAX_REPORTS_PER_MINUTE} raporttia "
        f"aikaleimalla {stamp}. Odota minuutti tai siirrä vanhat raportit "
        "talteen -- vanhaa raporttia ei ylikirjoiteta."
    )


# -- Manifesti -------------------------------------------------------------------


def _inputs(archive: ArchivePaths, team_key: str) -> list[ManifestInput]:
    """Syöte: joukkueen ``aggregate``-manifesti.

    Puuttuva manifesti ei kaada ajoa. ``report.json`` on olemassa ja luettu,
    joten raportti on kirjoitettavissa; vain jäljitettävyys jää vajaaksi, ja
    se on pienempi haitta kuin se, ettei käyttäjä saa pyytämäänsä raporttia
    lainkaan. Manifestiton syöte merkitään tyhjällä tiivisteellä, jolloin se
    erottuu tunnetusta.
    """
    manifest = Manifest.read_if_exists(archive.resolve(report_manifest(team_key)))
    if manifest is None:
        return [
            ManifestInput(
                result_id=str(PurePosixPath("aggregates") / team_key), sha256=""
            )
        ]
    return [ManifestInput(result_id=manifest.result_id, sha256=manifest.fingerprint())]


def _params_hash(settings: ReportSettings) -> str:
    """Parametrihash raporttimallista ja vaiheen omasta asetusosiosta.

    Kynnykset, aikaikkunat ja otannat tulevat ``report.json``ista, joten
    niitä ei ole täällä. Vaiheen omat asetukset ovat: ``[report]``-osio
    (karsintasäännöt, Story 2.13) päättää, mitkä rivit raporttiin
    kirjoitetaan, joten sen säätäminen tuottaa eri raportin samasta
    ``report.json``ista.

    Osio menee hashiin **kokonaisena** (``model_dump``), ei kenttä
    kerrallaan: luettelo luetuista kentistä vaatisi ylläpitoa ja vanhenisi
    hiljaa juuri silloin, kun osioon lisätään kuudes sääntö. Sama valinta
    kuin ``aggregate``ssa ``[aggregate]``-osion kanssa.

    **Hash ei kata koko ohjelman puolta.** :mod:`pappascout.render.view`
    valitsee jokaisen rivin ja sanamuodon, eikä sen muuttaminen näy tässä
    hashissa mitenkään -- kahden raportin identtiset manifestit eivät siis
    todista niiden syntyneen samasta koodista. Vanhentunut raportti ei silti
    pääse ulos, koska tätä vaihetta ei koskaan ohiteta manifestin perusteella
    (ks. :func:`run`); puute on kirjattu suunnittelun ``deferred-work.md``:hyn,
    joka asuu BMAD-tuotoksissa (``_bmad-output/implementation-artifacts/``)
    eikä tässä repossa. Asetukset
    ovat eri asia kuin se puute, ja siksi ne korjattiin heti: koodimuutos
    näkyy versionhallinnassa, mutta säädetty asetustiedosto ei näy missään,
    jos se ei näy manifestissa.
    """
    return compute_params_hash(
        {
            "render": {"template_sha256": template_digest()},
            "report": settings.model_dump(mode="json"),
        }
    )


# -- Tulosteen luvut -------------------------------------------------------------


def _stats(report: Report, markdown: str) -> dict[str, Any]:
    """Luvut, jotka ``cli`` näyttää käyttäjälle."""
    return {
        "team_key": report.team.key,
        "team_name_known": report.team.display_name != report.team.key,
        "demos": report.sample.demos,
        "rounds": report.sample.rounds,
        "maps": [entry.map_name for entry in report.maps],
        "missing_demos": len(report.missing_demos),
        "unclassified": report.unclassified_rounds,
        "lines": markdown.count("\n"),
        "characters": len(markdown),
        "pappascout": __version__,
    }
