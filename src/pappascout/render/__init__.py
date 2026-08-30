"""``render`` -- ``report.json`` Markdowniksi. **Ei laske mitään.**

Paketti on raportin *esityskerros*: se valitsee, mitä
:class:`~pappascout.domain.report.Report`in luvuista sanotaan, ja Jinja2-malli
:data:`TEMPLATE_NAME` päättää missä muodossa. Jokainen luku tulee sellaisenaan
``report.json``ista -- täällä ei summata, keskiarvoisteta eikä johdeta uusia
lukuja. Jos raportti tarvitsee luvun, jota mallissa ei ole, korjaus tehdään
``aggregate``-vaiheeseen (Story 2.3), ei tänne.

Työnjako: **koodi valitsee mitä sanotaan, malli miten**
--------------------------------------------------------
:mod:`pappascout.render.view` rakentaa raportista näkymämallin -- rivit,
väitteet ja niiden otannat -- ja malli latoo ne. Raportin muoto muuttuu
varmasti, kun ihminen lukee ensimmäisen version, ja tekstitiedostona sen voi
muuttaa koskematta valintalogiikkaan.

Malli on osa tulosta, ei ympäristöä
-----------------------------------
:func:`template_digest` on mallin sisällön tiiviste, ja se menee
``render``-vaiheen parametrihashiin. Ilman sitä mallin muokkaaminen muuttaisi
raportin sisältöä ilman että manifestissa näkyisi mitään -- sama
epäonnistumistapa, jonka
:func:`~pappascout.constants.weapon_classification_digest` estää parsinnassa.

**Tiiviste ja renderöinti lukevat saman tekstin.** Kumpaakaan ei
välimuistiteta: aiemmin tiiviste oli ``lru_cache``ssa ja Jinjan
``FileSystemLoader`` latasi mallin uudelleen automaattisesti, jolloin ajon
aikana muokattu malli olisi tuottanut uuden raportin vanhalla tiivisteellä --
täsmälleen se tila, jonka tiiviste on lisätty estämään. Malli on muutama
kilotavu ja luetaan kerran ajossa, joten välimuistilla ei ole mitään
voitettavaa.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, StrictUndefined, TemplateError

from pappascout.domain.report import Report
from pappascout.errors import PappascoutError
from pappascout.render.view import ReportView, build_view, round_list_demo_ids

__all__ = [
    "TEMPLATE_NAME",
    "template_path",
    "template_text",
    "template_digest",
    "render_report",
    "build_view",
    "round_list_demo_ids",
    "ReportView",
]

#: Raporttimallin tiedostonimi tämän paketin hakemistossa.
TEMPLATE_NAME = "report.md.j2"


def template_path() -> Path:
    """Raporttimallin polku.

    Malli luetaan paketin omasta hakemistosta eikä arkistosta: se on osa
    ohjelmaa, ei käyttäjän dataa. Arkistossa oleva malli tarkoittaisi, että
    kahdella koneella voi olla eri raporttimuoto samalla ohjelmaversiolla.
    """
    return Path(__file__).resolve().parent / TEMPLATE_NAME


def template_text() -> str:
    """Mallin sisältö tekstinä, rivinvaihdot normalisoituna.

    Normalisointi tehdään **täällä** eikä tiivistefunktiossa, jotta tiiviste
    ja renderöinti näkevät kirjaimellisesti saman merkkijonon: muuten sama
    malli antaisi eri tiivisteen riippuen siitä, onko työkopio haettu CRLF-
    vai LF-päätteillä.
    """
    return template_path().read_text(encoding="utf-8").replace("\r\n", "\n")


def template_digest() -> str:
    """Mallin sisällön sha256.

    Menee ``render``-vaiheen parametrihashiin, jolloin mallin muokkaaminen
    näkyy manifestissa.

    Returns:
        64 merkin heksadesimaalinen tiiviste **siitä samasta tekstistä**, jonka
        :func:`render_report` latoo.
    """
    return hashlib.sha256(template_text().encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """Jinja-ympäristö ilman lataajaa.

    Malli annetaan ``from_string``illa :func:`template_text`istä, joten
    ympäristöllä ei ole omaa käsitystä tiedostosta eikä siten omaa
    välimuistia, joka voisi erkaantua tiivisteestä.

    ``StrictUndefined`` on olennainen: oletusarvoinen ``Undefined`` latoo
    puuttuvan kentän **tyhjänä merkkijonona**, jolloin mallin kirjoitusvirhe
    tuottaisi raportin, josta yksi rivi puuttuu hiljaa. Se on juuri se
    epäonnistumistapa, jonka "mikään ei katoa hiljaa" kieltää.
    """
    return Environment(
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,  # noqa: S701 - Markdownia, ei HTML:ää
    )


def render_report(report: Report, *, round_list_paths: Sequence[str] = ()) -> str:
    """Muotoile raportti Markdowniksi.

    Args:
        report: ``aggregate``-vaiheen tulos sellaisenaan.
        round_list_paths: Kierroslistojen polut kierrosliitettä varten. Vaihe
            ratkaisee ne ``archive.paths``ista; tämä kerros ei näe arkistoa.

    Returns:
        Yksi Markdown-dokumentti, joka päättyy rivinvaihtoon.

    Raises:
        ~pappascout.errors.PappascoutError: Jos malli on rikki tai siinä
            viitataan kenttään, jota näkymässä ei ole. Jinjan oma
            ``TemplateError`` ei periydy ``PappascoutError``ista, joten ilman
            käärettä vaiheen dokumentoitu virhesopimus ei pitäisi ja käyttäjä
            näkisi englanninkielisen pinojäljen.
    """
    view = build_view(report, round_list_paths=round_list_paths)
    try:
        template = _environment().from_string(template_text())
        text = template.render(view=view)
    except TemplateError as exc:
        raise PappascoutError(
            f"Raporttimallin {template_path()} latominen epäonnistui: {exc}\n"
            "Kyseessä on ohjelmavirhe raporttimallissa, ei käyttäjän "
            "aineistossa. Palauta malli versionhallinnasta."
        ) from exc
    return _tidy(text)


def _tidy(text: str) -> str:
    """Siivoa mallin väistämättä tuottama tyhjätila.

    Ehtolauseet jättävät peräkkäisiä tyhjiä rivejä aina kun osio jää pois.
    Siivous on täällä eikä mallissa, jotta mallin ehdot pysyvät luettavina --
    hinta on, että dokumentin lopullinen muoto syntyy kahdessa paikassa, ja
    juuri siksi siitä on golden-testi.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    cleaned: list[str] = []
    for line in lines:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line.rstrip())
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned) + "\n"
