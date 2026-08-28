"""Portit, joiden takaa vaiheet näkevät ulkomaailman (AD-8).

Vaihe ei saa tuntea demoparser2:ta. Se ottaa parametrikseen tämän moduulin
protokollan, ja oikea toteutus annetaan sille kutsussa. Testeissä tilalle
annetaan feikki, joka rakentaa taulun käsin -- silloin vaiheen logiikan voi
testata ilman 233 MB:n demoa.

Protokolla on ``typing.Protocol``, ei kantaluokka: toteutuksen ei tarvitse
periytyä mistään, ja tuonti pysyy kevyenä.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import polars as pl

from pappascout.domain.rounds import REQUIRED_COLUMNS as _NUMBERING_COLUMNS
from pappascout.domain.schemas import ROUNDS

__all__ = ["DemoRoundsParser", "ParseDiagnostics", "ROUNDS_ADAPTER_COLUMNS"]

#: Sarakkeet, jotka :meth:`DemoRoundsParser.parse_rounds` palauttaa
#: **tarkalleen** -- ei enempää eikä vähempää.
#:
#: Kaksi eroa ``ROUNDS``-sopimukseen:
#:
#: ``map_demo_id`` puuttuu
#:     Adapteri saa pelkän tiedostopolun eikä voi tietää arkiston tunnistetta.
#:     Sen liittää ``stages.parse``.
#: ``score_start`` ja ``score_end`` ovat mukana
#:     Ne ovat kierroksen alun ja lopun **yhteispistemäärä**, ja
#:     :func:`~pappascout.domain.rounds.mark_played_rounds` päättää niistä,
#:     onko kierros pelattu. ``stages.parse`` pudottaa ne ennen kirjoitusta,
#:     joten levylle ne eivät päädy. Ilman niitä sopimuksessa toinen adapteri
#:     läpäisisi vaiheen saraketarkistuksen ja kaatuisi vasta domain-kerroksessa.
ROUNDS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    [name for name in ROUNDS if name != "map_demo_id"]
    + [name for name in _NUMBERING_COLUMNS if name not in ROUNDS]
)


@dataclass(frozen=True)
class ParseDiagnostics:
    """Havainnot, jotka eivät mahdu ``ROUNDS``-sopimukseen.

    Tauluun ei saa lisätä sarakkeita ilman skeemamuutosta (AD-2), mutta
    käyttäjälle on silti kerrottava, kun jokin arvo on oletus eikä mittaus.
    Adapteri saa tarjota tämän valinnaisena ``diagnostics``-attribuuttina;
    vaihe lukee sen varovasti (``getattr``), joten portin toteutuksen ei ole
    pakko tarjota sitä.

    Attributes:
        tick_rate: Käytetty tickrate.
        tick_rate_measured: ``True``, jos tickrate mitattiin demosta;
            ``False``, jos jouduttiin turvautumaan oletukseen.
        rounds_seen: Demosta löytyneiden kierrosrajojen määrä, pelatut ja
            pelaamattomat yhteensä.
    """

    tick_rate: float
    tick_rate_measured: bool
    rounds_seen: int


@runtime_checkable
class DemoRoundsParser(Protocol):
    """Portti, joka lukee demosta kierrostaulun.

    Toteutuksen on palautettava **havaitut** arvot sellaisenaan: ei
    kierrostyyppiluokittelua, ei loss countia, ei muuta johdettua. Ainoa
    päättely, joka tähän kuuluu, on kierrosrajojen tunnistaminen.
    """

    def parse_rounds(self, path: Path) -> pl.DataFrame:
        """Lue demo ja palauta kierrostaulu.

        Args:
            path: Demotiedosto, joko ``.dem`` tai pakattu ``.dem.zst`` /
                ``.dem.gz``.

        Returns:
            Pitkä taulu, kaksi riviä per kierros (yksi kummallekin
            joukkueelle). Sarakkeet ja tyypit ovat täsmälleen
            :data:`ROUNDS_ADAPTER_COLUMNS`. ``round_no`` on kaikilla riveillä
            ``null`` -- numeroinnin päättää ``domain.rounds.mark_played_rounds``,
            jota vain ``stages.parse`` kutsuu.

        Raises:
            ~pappascout.errors.ParseError: Jos tiedosto ei ole CS2-demo tai
                sitä ei voi lukea. Viesti on suomeksi ja kertoo, mitä tehdä.
        """
        ...
