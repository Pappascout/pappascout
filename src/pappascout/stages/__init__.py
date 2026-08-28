"""Putken vaiheet: tiedostosta tiedostoon (AD-1).

Jokainen vaihe on funktio ``run(settings, archive, unit, *ports) -> StageResult``,
jonka syöte ja tulos ovat arkiston tiedostoja. Vaihe ei kutsu toista vaihetta
eikä kirjoita toisen vaiheen tulosalueelle; järjestyksestä päättää ``pipeline``.

Vaihe saa parametrikseen **vain oman asetusosionsa** (AD-3). Se ei siis pysty
lukemaan muita osioita, ja siksi esimerkiksi ``[thresholds]``-arvon muuttaminen
ei voi vaikuttaa ``parse``-vaiheen tulokseen eikä sen parametrihashiin.

Tämä paketti on myös se kerros, jonka kautta ``cli`` koskee arkistoon:
riippuvuusnuoli on ``cli -> stages -> {domain, adapters, archive}``, joten
komentorivi ei tuo ``archive``- eikä ``adapters``-pakettia itse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pappascout.archive.paths import ArchivePaths
from pappascout.constants import UnitStatus
from pappascout.domain.models import ProjectSettings

__all__ = ["StageResult", "archive_paths"]


@dataclass(frozen=True)
class StageResult:
    """Yhden vaiheen ja yhden yksikön ajon tulos.

    Vaihe ei tulosta mitään itse: se palauttaa tämän, ja ``cli`` päättää mitä
    käyttäjälle näytetään. Sama vaihe toimii siten myös web-kuoren takana.

    Attributes:
        stage: Vaiheen nimi, esimerkiksi ``"parse"``.
        unit: Käsitelty yksikkö, ``parse``-vaiheessa ``map_demo_id``.
        status: Yksikön tila (AD-9).
        skipped: Ohitettiinko vaihe täsmäävän manifestin perusteella.
        outputs: Kirjoitetut tiedostot arkiston sisäisinä suhteellisina
            polkuina. Ohitetussa ajossa nämä ovat aiemman ajon tiedostot.
        manifest_path: Manifestin polku arkiston sisällä.
        reason: Suomenkielinen selitys muulle kuin ``ok``-tilalle tai
            ohitukselle.
        duration_s: Ajoaika sekunteina.
        stats: Vaihekohtaiset luvut käyttäjän tulostetta varten, esimerkiksi
            kierrosten määrä. Vapaamuotoinen, koska jokainen vaihe kertoo eri
            asian.
    """

    stage: str
    unit: str
    status: UnitStatus
    skipped: bool
    outputs: tuple[PurePosixPath, ...] = ()
    manifest_path: PurePosixPath | None = None
    reason: str | None = None
    duration_s: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)


def archive_paths(project: ProjectSettings) -> ArchivePaths:
    """Rakenna arkistopolut ``[project]``-osiosta.

    Tämä on ``cli``:n ainoa tie arkistoon: komentorivi ei tuo ``archive``-
    pakettia itse, vaan pyytää polut tästä kerroksesta.
    """
    return ArchivePaths.from_settings(project.archive_root)
