"""Arkiston hakemistorakenne (AD-7).

Puu on lukittu spinen konventiotaulukossa::

    raw/faceit/                                  HTTP-välimuisti, saa tyhjentää
    index/teams.json                             kirjoittaa vain select
    index/matches.json                           kirjoittaa vain discover
    index/selections/<team_key>.json             kirjoittaa vain select
    index/next_opponent/<team_key>.json          kirjoittaa vain discover
    demos/<map_demo_id>.dem.zst  + .meta.json    kirjoittaa vain fetch / import
    parsed/<map_demo_id>/{ticks,events,rounds,lineups,deaths}.parquet + manifest
    classified/<team_key>/<map_demo_id>.parquet + .md + manifest
    aggregates/<team_key>/report.json
    reports/<team_key>/<YYYY-MM-DDTHHMM>-<team_slug>.md + sama nimi .manifest.json
    import/                                      saapuvien kansio
    logs/<host>/
    .lock

Moduuli tarjoaa polut kahdessa muodossa. Modulitason funktiot palauttavat
**suhteellisen** ``PurePosixPath``-polun, joka on ainoa muoto, jonka saa
tallentaa manifestiin tai indeksiin -- absoluuttinen polku rikkoisi arkiston
toisella koneella. :class:`ArchivePaths` liittää juuren eteen ja palauttaa
``Path``-olion, jota käytetään varsinaiseen I/O:hon.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pappascout.errors import PappascoutError

__all__ = [
    "ArchivePaths",
    "ARCHIVE_ROOT_ENV_VAR",
    "safe_component",
    "DEMO_SUFFIXES",
    "DEFAULT_DEMO_SUFFIX",
    "PARSED_TABLES",
    "LOCK_FILE",
    "raw_faceit_dir",
    "index_dir",
    "teams_index",
    "matches_index",
    "selection",
    "next_opponent",
    "demo",
    "demo_meta",
    "parsed_dir",
    "parsed_table",
    "parsed_manifest",
    "classified",
    "classified_round_list",
    "classified_manifest",
    "report_json",
    "report_manifest",
    "reports_dir",
    "REPORT_TIMESTAMP_FORMAT",
    "MAX_REPORTS_PER_MINUTE",
    "report_name",
    "report_markdown",
    "render_manifest",
    "import_dir",
    "logs_dir",
]

#: FACEIT tarjoaa demot zstd-pakattuina (todettu 2026-08-28); ``.dem.gz`` on
#: käsin tuotujen tiedostojen varamuoto.
DEFAULT_DEMO_SUFFIX = ".dem.zst"
DEMO_SUFFIXES: tuple[str, ...] = (".dem.zst", ".dem.gz", ".dem")

#: ``parse``-vaiheen kirjoittamat taulut, yksi demoa kohden. Luettelo on
#: portinvartija: nimi, jota ei ole täällä, ei voi päätyä arkiston polkuun.
PARSED_TABLES: tuple[str, ...] = (
    "rounds",
    "ticks",
    "events",
    "lineups",
    "deaths",
    "callouts",
    "match",
)

LOCK_FILE = PurePosixPath(".lock")

_MANIFEST_SUFFIX = ".manifest.json"

#: Ympäristömuuttuja, jolla arkiston juuren voi ylikirjoittaa koneittain ilman
#: että versioitua settings.tomlia tarvitsee muokata.
ARCHIVE_ROOT_ENV_VAR = "PAPPASCOUT_ARCHIVE_ROOT"

#: Polun osaan kelpaavat merkit. Tunnisteet (team_key, map_demo_id, host) tulevat
#: FACEITista ja käyttäjän asetuksista, joten niitä ei interpoloida polkuun
#: tarkistamatta -- muuten ".." karkaisi arkiston juuresta.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


def safe_component(value: str, kind: str) -> str:
    """Tarkista, että tunniste kelpaa polun osaksi sellaisenaan.

    Args:
        value: Tarkistettava tunniste.
        kind: Tunnisteen laji virheilmoitusta varten, esimerkiksi ``"team_key"``.

    Returns:
        Sama arvo muuttumattomana.

    Raises:
        PappascoutError: Jos arvo sisältää polkuerottimia, on tyhjä, on ``.``
            tai ``..``, tai on liian pitkä.
    """
    if not isinstance(value, str) or not _SAFE_COMPONENT.match(value):
        raise PappascoutError(
            f"Tunniste {kind}={value!r} ei kelpaa arkiston polun osaksi. "
            "Sallittuja ovat kirjaimet, numerot sekä merkit _ . - "
            "(enintään 120 merkkiä)."
        )
    if value in {".", ".."}:
        raise PappascoutError(
            f"Tunniste {kind}={value!r} ei kelpaa arkiston polun osaksi: "
            "se osoittaisi arkiston juuren ulkopuolelle."
        )
    return value


def raw_faceit_dir() -> PurePosixPath:
    """HTTP-välimuisti. Saa tyhjentää milloin tahansa."""
    return PurePosixPath("raw/faceit")


def index_dir() -> PurePosixPath:
    return PurePosixPath("index")


def teams_index() -> PurePosixPath:
    """Joukkueindeksi. Kirjoittaa vain ``select``."""
    return index_dir() / "teams.json"


def matches_index() -> PurePosixPath:
    """Otteluindeksi. Kirjoittaa vain ``discover``."""
    return index_dir() / "matches.json"


def selection(team_key: str) -> PurePosixPath:
    """Joukkueen otteluvalinta ja rosterikynnys. Kirjoittaa vain ``select``."""
    return index_dir() / "selections" / f"{safe_component(team_key, 'team_key')}.json"


def next_opponent(team_key: str) -> PurePosixPath:
    """Seuraava vastustaja. Kirjoittaa vain ``discover``."""
    return index_dir() / "next_opponent" / f"{safe_component(team_key, 'team_key')}.json"


def demo(map_demo_id: str, suffix: str = DEFAULT_DEMO_SUFFIX) -> PurePosixPath:
    """Pakattu demotiedosto."""
    return PurePosixPath("demos") / f"{safe_component(map_demo_id, 'map_demo_id')}{suffix}"


def demo_meta(map_demo_id: str) -> PurePosixPath:
    """Demon metatiedot: ``sha256``, ``size``, ``source``, ``fetched_at``.

    Manifestit lukevat tiivisteen tästä tiedostosta eivätkä laske sitä
    uudelleen demosta -- 233 MB:n hashaus jokaisella ajolla olisi liian hidas.
    """
    return PurePosixPath("demos") / f"{safe_component(map_demo_id, 'map_demo_id')}.meta.json"


def parsed_dir(map_demo_id: str) -> PurePosixPath:
    return PurePosixPath("parsed") / safe_component(map_demo_id, "map_demo_id")


def parsed_table(map_demo_id: str, table: str) -> PurePosixPath:
    """Yksi parsittu taulu yhdelle demolle; ks. :data:`PARSED_TABLES`."""
    if table not in PARSED_TABLES:
        raise ValueError(
            f"Tuntematon parsittu taulu {table!r}. "
            f"Sallitut: {', '.join(PARSED_TABLES)}."
        )
    return parsed_dir(map_demo_id) / f"{table}.parquet"


def parsed_manifest(map_demo_id: str) -> PurePosixPath:
    return parsed_dir(map_demo_id) / f"parse{_MANIFEST_SUFFIX}"


def classified(team_key: str, map_demo_id: str) -> PurePosixPath:
    return (
        PurePosixPath("classified")
        / safe_component(team_key, "team_key")
        / f"{safe_component(map_demo_id, 'map_demo_id')}.parquet"
    )


def classified_round_list(team_key: str, map_demo_id: str) -> PurePosixPath:
    """Kierroslista Markdownina, ``classify``-vaiheen toinen tulos.

    Sama lista kuin ``--show`` tulostaa, mutta tiedostona: Veeti lukee sen
    demon rinnalla ja tarkistaa jokaisen päätöksen perustelun ja lähtöarvot.
    Tiedosto on ``classify``-vaiheen omassa hakemistossa, koska ``reports/`` on
    ``render``-vaiheen aluetta eikä vaihe kirjoita toisen vaiheen tulosalueelle.
    """
    return (
        PurePosixPath("classified")
        / safe_component(team_key, "team_key")
        / f"{safe_component(map_demo_id, 'map_demo_id')}.md"
    )


def classified_manifest(team_key: str, map_demo_id: str) -> PurePosixPath:
    return (
        PurePosixPath("classified")
        / safe_component(team_key, "team_key")
        / f"{safe_component(map_demo_id, 'map_demo_id')}{_MANIFEST_SUFFIX}"
    )


def report_json(team_key: str) -> PurePosixPath:
    """``aggregate``-vaiheen tulos: ``Report``-malli JSONina."""
    return PurePosixPath("aggregates") / safe_component(team_key, "team_key") / "report.json"


def report_manifest(team_key: str) -> PurePosixPath:
    return (
        PurePosixPath("aggregates")
        / safe_component(team_key, "team_key")
        / f"report{_MANIFEST_SUFFIX}"
    )


def reports_dir(team_key: str) -> PurePosixPath:
    """Markdown-raporttien hakemisto."""
    return PurePosixPath("reports") / safe_component(team_key, "team_key")


#: Raportin tiedostonimen aikaleima **paikallista aikaa**, minuutin
#: tarkkuudella. Paikallinen siksi, että tiedostonimi on ainoa asia, jonka
#: käyttäjä tästä näkee, ja hän muistaa milloin ajoi komennon -- ei sitä, mitä
#: kello oli silloin UTC:ssä. Raportin sisällä oleva ``generated_at`` on UTC,
#: koska se on datan aikaleima eikä käyttöliittymää.
REPORT_TIMESTAMP_FORMAT = "%Y-%m-%dT%H%M"


#: Suurin järjestysluku saman minuutin raporteille. Kaksinumeroinen, koska
#: nimen järjestysluku on nollatäytetty (ks. :func:`report_name`): kolminumeroinen
#: raja rikkoisi täytön ja palauttaisi lajittelujärjestyksen sekaisin.
MAX_REPORTS_PER_MINUTE = 99


def report_name(timestamp: str, team_slug: str, ordinal: int = 1) -> str:
    """Raportin tiedostonimi: ``<aikaleima>-<slug>.md``.

    Args:
        timestamp: Aikaleima muodossa :data:`REPORT_TIMESTAMP_FORMAT`.
        team_slug: Joukkueen tiedostonimeen kelpaava muoto.
        ordinal: Monesko raportti saman minuutin sisällä. Ensimmäinen on
            nimetön, seuraavat saavat **nollatäytetyn** päätteen ``-02``,
            ``-03``. Täyttö on lajittelua varten: ilman sitä hakemistolistaus
            järjestäisi nimet ``-10, -100, -11, -2``, eli uusin raportti ei
            olisi listan lopussa. **Yksikään aiempi raportti ei ylikirjoitu**,
            koska uusi ajo ei koskaan osu vanhaan nimeen.

    Raises:
        ValueError: Jos ``ordinal`` on rajojen ``1``..
            :data:`MAX_REPORTS_PER_MINUTE` ulkopuolella.
    """
    if not 1 <= ordinal <= MAX_REPORTS_PER_MINUTE:
        raise ValueError(
            f"Raportin järjestysluvun on oltava 1..{MAX_REPORTS_PER_MINUTE}, "
            f"oli {ordinal}."
        )
    suffix = "" if ordinal == 1 else f"-{ordinal:02d}"
    return f"{timestamp}-{team_slug}{suffix}.md"


def report_markdown(team_key: str, filename: str) -> PurePosixPath:
    """Yksi Markdown-raportti. ``render``-vaiheen tulos."""
    return reports_dir(team_key) / safe_component(filename, "report_filename")


def render_manifest(team_key: str, report_filename: str) -> PurePosixPath:
    """Yhden raportin manifesti: ``<raportin nimi ilman .md>.manifest.json``.

    Manifesti on **jäljitettävyyttä varten, ei ohitusta**: raportti
    kirjoitetaan joka ajolla uudella nimellä, joten vaihetta ei koskaan
    ohiteta.

    **Manifesti on raporttikohtainen, ei joukkuekohtainen.** Yhteinen
    ``render.manifest.json`` kestäisi huonosti juuri sitä rinnakkaisuutta,
    jota varten tiedostonimi varataan atomisesti: kaksi yhtaikaista ajoa saisi
    kumpikin oman raporttinsa, mutta viimeisenä kirjoittava manifesti jäisi
    voimaan ja kuvaisi eri tiedostoa kuin se, jonka käyttäjä juuri sai.
    Raportin nimi on jo yksikäsitteinen, joten siitä johdettu manifestinimi on
    sitä myös.
    """
    stem = report_filename.removesuffix(".md")
    return (
        reports_dir(team_key)
        / f"{safe_component(stem, 'report_filename')}{_MANIFEST_SUFFIX}"
    )


def import_dir() -> PurePosixPath:
    """Saapuvien kansio, jota lukee vain ``pappascout import``."""
    return PurePosixPath("import")


def logs_dir(host: str) -> PurePosixPath:
    """Lokit per kone, jotta kaksi konetta ei kirjoita samaan tiedostoon."""
    return PurePosixPath("logs") / safe_component(host, "host")


#: Laajentamaton ympäristömuuttuja polussa: ``%NIMI%`` tai ``${NIMI}``.
#:
#: Paljas ``$NIMI`` on mukana vain muualla kuin Windowsilla. Windows-poluissa
#: dollari on laillinen hakemistonimen merkki (``$Recycle.Bin``,
#: hallintajaot), joten sen kieltäminen hylkäisi kelvollisia polkuja. Kaksi
#: yksiselitteistä muotoa riittää: juuri ``%NIMI%`` on se, jonka
#: ``settings.toml`` sisältää.
_UNEXPANDED_VAR = (
    re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    if os.name == "nt"
    else re.compile(
        r"%([A-Za-z_][A-Za-z0-9_]*)%|\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"
    )
)


def _check_expanded(expanded: str, raw: str, source: str) -> None:
    """Kaadu, jos ympäristömuuttuja jäi laajentamatta.

    Args:
        expanded: ``os.path.expandvars``in tulos.
        raw: Alkuperäinen arvo, virheviestiä varten.
        source: Mistä arvo tuli, jotta käyttäjä tietää mitä korjata.

    Raises:
        PappascoutError: Viesti nimeää puuttuvan muuttujan.
    """
    match = _UNEXPANDED_VAR.search(expanded)
    if match is None:
        return
    name = match.group(1) or match.group(2)
    raise PappascoutError(
        f"Arkiston juuren polussa on ympäristömuuttuja {name}, jota ei ole "
        f"asetettu tällä koneella.\n"
        f"Arvo tulee kohteesta {source} ja on {raw!r}.\n"
        f"Aseta {name} tai kirjoita polku kokonaan auki. Ilman tätä "
        "tarkistusta arkisto kirjoitettaisiin hakemistoon, jonka nimi on "
        f"kirjaimellisesti {match.group(0)!r}."
    )


@dataclass(frozen=True)
class ArchivePaths:
    """Arkiston juuri ja siihen sidotut absoluuttiset polut.

    Kaikki modulitason polkufunktiot ovat saatavilla metodeina, jotka palauttavat
    ``Path``-olion. Suhteellisen muodon saa aina funktioista suoraan.
    """

    root: Path

    @classmethod
    def from_settings(cls, archive_root: Path | str) -> ArchivePaths:
        """Rakenna arkistopolut asetuksen arvosta.

        Polku laajennetaan kahdesti, jotta sama versioitu ``settings.toml``
        toimii molemmilla koneilla: ``%USERPROFILE%``-tyyliset
        ympäristömuuttujat ja ``~`` korvataan koneen omilla arvoilla.

        Ympäristömuuttuja ``PAPPASCOUT_ARCHIVE_ROOT`` ylikirjoittaa asetuksen
        kokonaan -- se on tapa osoittaa toinen arkisto muokkaamatta versioitua
        tiedostoa.

        Raises:
            PappascoutError: Jos laajennuksen jälkeen polussa on yhä
                laajentamaton ympäristömuuttuja. ``os.path.expandvars``
                **jättää ``%NIMI%``:n sellaisenaan**, jos muuttujaa ei ole --
                se ei nosta virhettä eikä palauta tyhjää. Ilman tätä
                tarkistusta ajo loisi hakemiston, jonka nimi on kirjaimellisesti
                ``%USERPROFILE%``, kirjoittaisi koko arkiston sinne ja näyttäisi
                onnistuneen. Kahden koneen arkisto hajoaisi hiljaa.
        """
        override = os.environ.get(ARCHIVE_ROOT_ENV_VAR)
        raw = override if override else str(archive_root)
        source = (
            f"ympäristömuuttuja {ARCHIVE_ROOT_ENV_VAR}"
            if override
            else "asetus [project].archive_root"
        )
        expanded = os.path.expandvars(str(raw))
        _check_expanded(expanded, raw, source)
        return cls(root=Path(expanded).expanduser())

    def resolve(self, relative: PurePosixPath | str) -> Path:
        """Liitä suhteellinen arkistopolku juureen.

        Raises:
            PappascoutError: Jos polku on absoluuttinen. Manifestit ja indeksit
                saavat sisältää vain suhteellisia polkuja, joten absoluuttinen
                polku on aina merkki virheestä eikä sitä hiljaisesti hyväksytä.
        """
        candidate = Path(str(relative))
        # Windowsilla "/etc/passwd" ei ole is_absolute() (asema puuttuu), mutta
        # sen root on "\\" -- se on silti pako arkiston juuresta.
        if candidate.is_absolute() or candidate.drive or candidate.root:
            raise PappascoutError(
                f"Arkistopolun {relative!r} pitää olla suhteellinen arkiston "
                "juureen nähden. Absoluuttinen polku rikkoisi arkiston toisella "
                "koneella."
            )
        if ".." in candidate.parts:
            raise PappascoutError(
                f"Arkistopolku {relative!r} sisältää '..' eikä siksi pysy "
                "arkiston juuren sisällä."
            )
        return self.root / candidate

    def relative(self, path: Path | str) -> PurePosixPath:
        """Muunna absoluuttinen polku arkiston sisäiseen suhteelliseen muotoon.

        Raises:
            ValueError: Jos polku ei ole arkiston sisällä.
        """
        rel = Path(path).resolve().relative_to(self.root.resolve())
        return PurePosixPath(rel.as_posix())

    # -- Käteviä pikakuljetuksia ------------------------------------------
    def raw_faceit(self) -> Path:
        return self.resolve(raw_faceit_dir())

    def teams_index(self) -> Path:
        return self.resolve(teams_index())

    def matches_index(self) -> Path:
        return self.resolve(matches_index())

    def selection(self, team_key: str) -> Path:
        return self.resolve(selection(team_key))

    def next_opponent(self, team_key: str) -> Path:
        return self.resolve(next_opponent(team_key))

    def demo(self, map_demo_id: str, suffix: str = DEFAULT_DEMO_SUFFIX) -> Path:
        return self.resolve(demo(map_demo_id, suffix))

    def find_demo(self, map_demo_id: str) -> Path | None:
        """Etsi demo tuetuista päätteistä. ``None``, jos demoa ei ole."""
        for suffix in DEMO_SUFFIXES:
            candidate = self.demo(map_demo_id, suffix)
            if candidate.is_file():
                return candidate
        return None

    def demo_meta(self, map_demo_id: str) -> Path:
        return self.resolve(demo_meta(map_demo_id))

    def parsed_table(self, map_demo_id: str, table: str) -> Path:
        return self.resolve(parsed_table(map_demo_id, table))

    def parsed_manifest(self, map_demo_id: str) -> Path:
        return self.resolve(parsed_manifest(map_demo_id))

    def classified(self, team_key: str, map_demo_id: str) -> Path:
        return self.resolve(classified(team_key, map_demo_id))

    def classified_round_list(self, team_key: str, map_demo_id: str) -> Path:
        return self.resolve(classified_round_list(team_key, map_demo_id))

    def classified_manifest(self, team_key: str, map_demo_id: str) -> Path:
        return self.resolve(classified_manifest(team_key, map_demo_id))

    def report_json(self, team_key: str) -> Path:
        return self.resolve(report_json(team_key))

    def report_manifest(self, team_key: str) -> Path:
        return self.resolve(report_manifest(team_key))

    def reports_dir(self, team_key: str) -> Path:
        return self.resolve(reports_dir(team_key))

    def report_markdown(self, team_key: str, filename: str) -> Path:
        return self.resolve(report_markdown(team_key, filename))

    def render_manifest(self, team_key: str, report_filename: str) -> Path:
        return self.resolve(render_manifest(team_key, report_filename))

    def import_dir(self) -> Path:
        return self.resolve(import_dir())

    def logs_dir(self, host: str) -> Path:
        return self.resolve(logs_dir(host))

    def lock_file(self) -> Path:
        return self.resolve(LOCK_FILE)

    # -- Tilatiedot --------------------------------------------------------
    def exists(self) -> bool:
        return self.root.is_dir()

    def total_size_bytes(self) -> int:
        """Arkiston yhteiskoko tavuina. ``0``, jos arkistoa ei ole vielä luotu."""
        if not self.root.is_dir():
            return 0
        total = 0
        for path in self.root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                # OneDriven pilvipaikkamerkki tai kesken siirtyvä tiedosto.
                continue
        return total
