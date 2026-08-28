"""demoparser2-toteutus kierros- ja näytepistetaululle (AD-8).

**Tämä on ainoa moduuli, jossa pelin propinimet esiintyvät.** Vaihe näkee vain
:class:`~pappascout.adapters.protocols.DemoParser`-portin, joten demoparser2:n
vaihtaminen tai päivittäminen ei kosketa putkea.

Kaikki alla käytetyt kentät on **todettu oikeasta demosta** (ks.
``_bmad-output/implementation-artifacts/demoparser2-kentat.md``), ei arvattu.

Kierrosrajat
------------
Kierros rajautuu kahden tapahtuman väliin:

``round_freeze_end``
    Ostoajan loppu. Tässä hetkessä luetaan **havaitut lähtöarvot**: raha,
    varustearvo freezetimen lopussa, kierroksen alun varustearvo ja puoli.
``round_end``
    Kierroksen ratkeaminen. Tässä hetkessä luetaan voittaja, voiton syy,
    eloonjääneet ja heidän varusteensa. Pelaajat eivät ole vielä syntyneet
    uudelleen -- ``round_officially_ended`` olisi liian myöhään, siinä
    kaikki kymmenen ovat jo elossa.

``round_end`` **on olemassa** demoparser2 0.42.0:ssa, vaikka se ei näy
``list_game_events()``-listalla. Se palauttaa sarakkeet ``round``, ``tick``,
``winner`` ja ``reason``, ja ensimmäinen rivi on tyhjä alkuarvo (tick 1).
``round`` on demon oma kierroslaskuri (Ancient 1..22, puukkokierros mukaan
lukien), ja se päätyy sellaisenaan ``round_raw``-sarakkeeseen.

Kierrosnumeroa **ei päätetä täällä**: adapteri palauttaa ``round_no``-sarakkeen
tyhjänä, ja ``stages.parse`` kutsuu ``domain.rounds.mark_played_rounds``ia.

Pistemäärän mittauspisteet
--------------------------
``score_start`` luetaan kierroksen omasta freezetime-ankkurista ja ``score_end``
**seuraavan kierroksen** ankkurista. Syy on puukkokierros: sen tuottaman
pisteen näkee vielä sen omassa ``round_end``-tickissä, mutta ``mp_restartgame``
nollaa sen heti perään -- oman lopputickin lukema väittäisi puukkokierrosta
pelatuksi. Seuraavan ankkurin lukema on nollauksen jälkeinen ja siksi oikea.

Viimeisellä kierroksella seuraavaa ankkuria ei ole, joten sen ``score_end``
luetaan omasta ``round_end``-tickistä. Se on turvallista: pistemäärä on siinä
hetkessä jo kasvanut (todennettu molemmista testidemoista), eikä nollausta
enää tule. Sama varalähde on käytössä myös silloin, kun seuraavalta
kierrokselta puuttuu ankkuri.

Kenen arvot summataan
---------------------
Freezetimen lopun summat (raha, käytetty raha, varustearvo, kierroksen alun
varustearvo) lasketaan vain niistä pelaajista, joiden **kaikki** nämä propit
ovat luettavissa, ja ``players_freeze_end`` on saman joukon koko. Jakaja on
siis aina sama joukko kuin osoittaja: kolmen pelaajan summa viidellä jaettuna
näyttäisi ecolta, vaikka joukkue olisi ostanut täyden.

Näytepisteet
------------
Sama lukukerta tuottaa myös ``ticks``-taulun: rivi per (pelaaja, kierros,
näytepiste). Näytepisteet valitsee :mod:`pappascout.domain.sampling`, joka on
puhdas funktio -- adapterin osuus on lukea propit valituilta tickeiltä ja
kertoa domainille, kummalla puolella kukin pelaaja on.

Kierrosrajat, kokoonpanot ja tickrate lasketaan **kerran** ja käytetään
molempiin tauluihin. Siksi portti palauttaa ne yhdessä
(:class:`~pappascout.adapters.protocols.DemoTables`): kaksi erillistä kutsua
tekisi kokoonpanojen tunnistuksen kahdesti, ja jos tulokset joskus eroaisivat,
``lineup_key`` olisi tauluissa eri eikä liitos enää osuisi.

Muistinkäyttö
-------------
Demoa ei ladata muistiin kokonaan. ``parse_ticks`` kutsutaan **vain
kierrosrajojen ja näytepisteiden tickeille** (Ancient: 44 + noin 100 tickiä,
~1 500 riviä), ei koko tickisarjalle. Kutsuja on kaksi eikä yksi, koska
näytepisteiden tickit riippuvat tickratesta, joka mitataan vasta
kierrosrajojen lukemista. Pakattu demo puretaan virtaavasti temp-tiedostoon.
"""

from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from pappascout.adapters.decompress import readable_demo
from pappascout.adapters.protocols import (
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
    ParseDiagnostics,
)
from pappascout.domain.sampling import (
    FIRST_CONTACT_SAMPLE,
    DamageEvent,
    RoundBounds,
    SamplePoint,
    first_contact_tick,
    sample_ticks,
    seconds_since_freeze_end,
)
from pappascout.domain.schemas import ROUNDS, TICKS
from pappascout.errors import ParseError

__all__ = [
    "Demoparser2Adapter",
    "TEAM_SIDES",
    "TICK_PROPS",
    "SAMPLE_TICK_PROPS",
    "DEFAULT_TICK_RATE",
    "TICK_RATE_MIN",
    "TICK_RATE_MAX",
]

# -- Pelin kentät -------------------------------------------------------------

_TEAM_NUM = "CCSPlayerController.m_iTeamNum"
_ACCOUNT = "CCSPlayerController.CCSPlayerController_InGameMoneyServices.m_iAccount"
_CASH_SPENT = (
    "CCSPlayerController.CCSPlayerController_InGameMoneyServices"
    ".m_iCashSpentThisRound"
)
_EQUIP_FREEZE_END = "CCSPlayerPawn.m_unFreezetimeEndEquipmentValue"
_EQUIP_ROUND_START = "CCSPlayerPawn.m_unRoundStartEquipmentValue"
_EQUIP_CURRENT = "CCSPlayerPawn.m_unCurrentEquipmentValue"
_LIFE_STATE = "CCSPlayerPawn.m_lifeState"
_TEAM_SCORE = "CCSTeam.m_iScore"
_ROUND_START_TIME = "CCSGameRulesProxy.CCSGameRules.m_fRoundStartTime"

#: Pelin oma aluenimi (``env_cs_place``). Noin kaksi kertaa karkeampi kuin
#: Total CS -callout; tyhjä merkkijono tarkoittaa aluetta, jolle peli ei anna
#: nimeä, ja se säilyy taulussa ``null``:na.
_PLACE_NAME = "CCSPlayerPawn.m_szLastPlaceName"

#: Pelaajan koordinaatit. demoparser2 palauttaa nämä valmiiksi float32:na.
_X = "X"
_Y = "Y"
_Z = "Z"

#: Propit, jotka luetaan kierrosrajojen tickeistä.
TICK_PROPS: tuple[str, ...] = (
    _TEAM_NUM,
    _ACCOUNT,
    _CASH_SPENT,
    _EQUIP_FREEZE_END,
    _EQUIP_ROUND_START,
    _EQUIP_CURRENT,
    _LIFE_STATE,
    _TEAM_SCORE,
    _ROUND_START_TIME,
)

#: Propit, jotka luetaan näytepisteiden tickeistä. Lyhyempi lista kuin
#: kierrosrajoilla: asetelmasta tarvitaan vain paikka, puoli ja elossaolo --
#: talousarvot ovat kierroksen ominaisuus, eivät hetken.
SAMPLE_TICK_PROPS: tuple[str, ...] = (
    _TEAM_NUM,
    _LIFE_STATE,
    _PLACE_NAME,
    _X,
    _Y,
    _Z,
)

#: ``m_iTeamNum`` -> puoli. 0 ja 1 ovat katsoja ja liittymätön, eivät joukkueita.
TEAM_SIDES: dict[int, str] = {2: "T", 3: "CT"}

#: Sarakkeet, jotka ``player_hurt``- ja ``player_death``-tapahtumissa on oltava.
#: Molemmat tarjoavat kaikki neljä demoparser2 0.42.0:ssa.
DAMAGE_COLUMNS: tuple[str, ...] = (
    "tick",
    "attacker_steamid",
    "user_steamid",
    "weapon",
)

#: Elossa olevan pelaajan ``m_lifeState``. Muut arvot ovat kuollut tai kuolemassa.
_ALIVE = 0

#: CS2:n oletustickrate. Käytetään vain jos demosta ei saa mitattua arvoa.
DEFAULT_TICK_RATE = 64.0

#: Järkevyysrajat mitatulle tickratelle. CS2:n palvelimet ajavat 64 tai 128
#: tickiä; näiden ulkopuolinen arvo on mittausvirhe (esimerkiksi kellon nollaus
#: kesken ottelun), ei totuus.
TICK_RATE_MIN = 16.0
TICK_RATE_MAX = 256.0


@dataclass
class _Lineup:
    """Yhden joukkueen kokoonpano yhdellä kartalla.

    ``members`` kasvaa kartan aikana, jos joukkue vaihtaa pelaajaa. Tunniste
    lasketaan kaikista kartalla pelanneista, jotta sama kokoonpano tuottaa
    saman avaimen ajosta toiseen.
    """

    members: set[str] = field(default_factory=set)

    def key(self) -> str:
        """Kokoonpanon tiiviste.

        Raises:
            ParseError: Jos kokoonpano on tyhjä. Tyhjän merkkijonon tiiviste
                olisi molemmilla joukkueilla sama, jolloin ``lineup_key`` ei
                erottaisi joukkueita lainkaan ja kaikki myöhempi ryhmittely
                menisi hiljaa väärin.
        """
        if not self.members:
            raise ParseError(
                "Demosta ei saatu tunnistettua kummankin joukkueen kokoonpanoa: "
                "toinen jäi tyhjäksi.\n"
                "Kierrosrajojen tickeistä ei löytynyt pelaajia molemmilta "
                "puolilta. Demo on todennäköisesti vioittunut tai katkennut."
            )
        raw = ",".join(sorted(self.members))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class _Segment:
    """Yksi kierros demossa, pelattu tai ei."""

    demo_round: int | None
    freeze_end_tick: int | None
    end_tick: int | None
    winner_side: str | None
    win_reason: str | None
    round_raw: int = 0


class Demoparser2Adapter:
    """Lukee kierros- ja näytepistetaulun demoparser2:lla.

    Toteuttaa :class:`~pappascout.adapters.protocols.DemoParser`-portin.

    Args:
        exclude_weapons: Aseet, jotka eivät kelpaa ensikontaktiksi
            (``[parse].first_contact_exclude_weapons``). Oletus on tarkoituksella
            tyhjä: adapteri ei lue asetuksia, vaan vaihe antaa listan.
        fallback_death: Saako ensikontakti tulla ``player_death``-tapahtumasta,
            jos kelvollista ``player_hurt``ia ei ole
            (``[parse].first_contact_fallback_death``).

    Attributes:
        diagnostics: Viimeisimmän parsinnan havainnot, jotka eivät mahdu
            taulusopimuksiin. ``None`` ennen ensimmäistä kutsua.
    """

    def __init__(
        self,
        *,
        exclude_weapons: Sequence[str] = (),
        fallback_death: bool = True,
    ) -> None:
        self.exclude_weapons = tuple(exclude_weapons)
        self.fallback_death = fallback_death
        self.diagnostics: ParseDiagnostics | None = None

    def parse_demo(
        self, path: Path, sample_seconds: Sequence[float]
    ) -> DemoTables:
        """Ks. portin dokumentaatio."""
        path = Path(path)
        with readable_demo(path) as demo_path:
            return self._parse(demo_path, path, tuple(sample_seconds))

    # -- Sisäinen ------------------------------------------------------------

    def _parse(
        self,
        demo_path: Path,
        alkuperainen: Path,
        sample_seconds: tuple[float, ...],
    ) -> DemoTables:
        parser = self._open(demo_path, alkuperainen)
        freeze_ticks = self._freeze_end_ticks(parser, alkuperainen)
        round_ends = self._round_ends(parser, alkuperainen)
        segments = self._segments(freeze_ticks, round_ends)

        if not segments:
            raise ParseError(
                f"Demosta {alkuperainen.name} ei löytynyt yhtään kierrosta.\n"
                "Tiedosto on todennäköisesti katkennut kesken latauksen. "
                "Lataa demo uudelleen."
            )

        wanted = sorted(
            {s.freeze_end_tick for s in segments if s.freeze_end_tick is not None}
            | {s.end_tick for s in segments if s.end_tick is not None}
        )
        by_tick = self._read_ticks(parser, wanted, alkuperainen)
        tick_rate, measured = self._tick_rate(by_tick, freeze_ticks)

        lineups = [_Lineup(), _Lineup()]
        sivut = self._assign_sides(segments, by_tick, lineups)
        avaimet = self._lineup_keys(lineups)
        rounds = self._build_frame(segments, by_tick, tick_rate, sivut, avaimet)

        pisteet, tuntemattomat = self._sample_points(
            parser,
            alkuperainen,
            segments,
            sivut,
            lineups,
            by_tick,
            tick_rate,
            sample_seconds,
        )
        ticks, vajaat = self._build_ticks_frame(
            pisteet, parser, alkuperainen, segments, sivut, avaimet
        )

        self.diagnostics = ParseDiagnostics(
            tick_rate=tick_rate,
            tick_rate_measured=measured,
            rounds_seen=len(segments),
            partial_samples=vajaat,
            unknown_side_events=tuntemattomat,
        )
        return DemoTables(rounds=rounds, ticks=ticks)

    def _open(self, demo_path: Path, alkuperainen: Path) -> Any:
        from demoparser2 import DemoParser as _Demoparser2

        try:
            return _Demoparser2(str(demo_path))
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demoa {alkuperainen.name} ei voitu avata: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut. Lataa demo uudelleen."
            ) from exc

    def _freeze_end_ticks(self, parser: Any, alkuperainen: Path) -> list[int]:
        frame = self._event(parser, "round_freeze_end", alkuperainen)
        if frame is None or "tick" not in frame.columns:
            return []
        return sorted({int(t) for t in frame["tick"].tolist()})

    def _round_ends(self, parser: Any, alkuperainen: Path) -> list[dict[str, Any]]:
        """Kierrosten päättymiset aikajärjestyksessä.

        Ensimmäinen rivi (tick 1, ``round`` 0, tyhjä voittaja) on demoparser2:n
        alkuarvo eikä kierros, joten se pudotetaan.
        """
        frame = self._event(parser, "round_end", alkuperainen)
        if frame is None or "tick" not in frame.columns:
            return []
        ends: list[dict[str, Any]] = []
        for row in frame.to_dict("records"):
            tick = _as_int(row.get("tick"))
            if tick is None or tick <= 1:
                continue
            ends.append(
                {
                    "tick": tick,
                    "round": _as_int(row.get("round")),
                    "winner": _as_side(row.get("winner")),
                    "reason": _as_str(row.get("reason")),
                }
            )
        ends.sort(key=lambda r: r["tick"])
        return ends

    def _event(self, parser: Any, name: str, alkuperainen: Path) -> Any:
        try:
            frame = parser.parse_event(name)
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {alkuperainen.name} tapahtumaa {name!r} ei voitu lukea: "
                f"{exc}\n"
                "Tiedosto on todennäköisesti katkennut. Lataa demo uudelleen."
            ) from exc
        if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
            return None
        return frame

    @staticmethod
    def _segments(
        freeze_ticks: list[int], round_ends: list[dict[str, Any]]
    ) -> list[_Segment]:
        """Paritä freezetime-ankkurit ja kierrosten päättymiset.

        Kierroksen ankkuri on viimeinen ``round_freeze_end`` ennen sen
        päättymistä. Jos ankkuria ei ole, kierros on silti mukana --
        ``freeze_end_tick`` jää tyhjäksi ja ``status`` kertoo syyn (AD-9).
        Jos ankkurin jälkeen ei tule päättymistä, demo on katkennut kesken
        kierroksen; kierros pysyy mukana, mutta ilman tulosta.
        """
        segments: list[_Segment] = []
        odottavat: list[int] = []
        i = 0
        for end in round_ends:
            while i < len(freeze_ticks) and freeze_ticks[i] < end["tick"]:
                odottavat.append(freeze_ticks[i])
                i += 1
            # Kaikki paitsi viimeinen ankkuri jäivät ilman päättymistä.
            for orpo in odottavat[:-1]:
                segments.append(_Segment(None, orpo, None, None, None))
            segments.append(
                _Segment(
                    demo_round=end["round"],
                    freeze_end_tick=odottavat[-1] if odottavat else None,
                    end_tick=end["tick"],
                    winner_side=end["winner"],
                    win_reason=end["reason"],
                )
            )
            odottavat = []
        for orpo in freeze_ticks[i:]:
            segments.append(_Segment(None, orpo, None, None, None))

        Demoparser2Adapter._assign_round_raw(segments)
        return segments

    @staticmethod
    def _assign_round_raw(segments: list[_Segment]) -> None:
        """Anna jokaiselle kierrokselle demon oma juokseva numero.

        Arvo tulee ``round_end``-tapahtuman ``round``-kentästä. Kierros, joka ei
        koskaan ratkennut, ei saa sitä kentästä; sille johdetaan naapureista
        arvo, joka säilyttää järjestyksen. Jos johdettu arvo törmäisi demon
        omaan arvoon, numerointi olisi epäjohdonmukainen eikä sitä hyväksytä.
        """
        if not segments:
            return
        raws: list[int | None] = [s.demo_round for s in segments]

        edellinen: int | None = None
        for index, arvo in enumerate(raws):
            if arvo is not None:
                edellinen = arvo
                continue
            if edellinen is not None:
                edellinen += 1
                raws[index] = edellinen

        ensimmainen = next((i for i, v in enumerate(raws) if v is not None), None)
        if ensimmainen is None:
            raws = list(range(1, len(raws) + 1))
        else:
            arvo = raws[ensimmainen]
            assert arvo is not None
            for index in range(ensimmainen - 1, -1, -1):
                arvo -= 1
                raws[index] = arvo

        for eka, toka in zip(raws, raws[1:]):
            if eka is None or toka is None or toka <= eka:
                raise ParseError(
                    "Demon oma kierrosnumerointi ei kasva tasaisesti "
                    f"({eka} -> {toka}).\n"
                    "Kierrosrajat eivät vastaa demoparser2:n round_end-numeroita, "
                    "joten kierroksia ei voi tunnistaa luotettavasti."
                )

        for segment, numero in zip(segments, raws):
            assert numero is not None
            segment.round_raw = numero

    def _read_ticks(
        self, parser: Any, ticks: list[int], alkuperainen: Path
    ) -> dict[int, list[dict[str, Any]]]:
        """Lue propit annetuista tickeistä ja ryhmittele tickin mukaan."""
        if not ticks:
            return {}
        try:
            frame = parser.parse_ticks(list(TICK_PROPS), ticks=ticks)
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {alkuperainen.name} tick-arvoja ei voitu lukea: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut tai demoparser2:n "
                "versio ei tunne näitä kenttiä. Aja: uv sync"
            ) from exc

        saadut = set(getattr(frame, "columns", ()))
        puuttuvat = [
            name for name in (*TICK_PROPS, "tick", "steamid") if name not in saadut
        ]
        if puuttuvat:
            raise ParseError(
                "demoparser2 ei palauttanut kaikkia pyydettyjä kenttiä demosta "
                f"{alkuperainen.name}. Puuttuu: {', '.join(puuttuvat)}.\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Ilman tarkistusta taulu näyttäisi kelvolliselta "
                "mutta olisi tyhjä. Päivitä adapters/demo_parser.py:n propinimet."
            )

        by_tick: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in frame.to_dict("records"):
            steamid = _as_str(row.get("steamid"))
            side = TEAM_SIDES.get(_as_int(row.get(_TEAM_NUM)) or -1)
            tick = _as_int(row.get("tick"))
            if steamid is None or side is None or tick is None:
                # Katsojat ja liittymättömät eivät ole kierroksen osapuolia.
                continue
            by_tick[tick].append(
                {
                    "steamid": steamid,
                    "side": side,
                    "account": _as_int(row.get(_ACCOUNT)),
                    "cash_spent": _as_int(row.get(_CASH_SPENT)),
                    "equip_freeze_end": _as_int(row.get(_EQUIP_FREEZE_END)),
                    "equip_round_start": _as_int(row.get(_EQUIP_ROUND_START)),
                    "equip_current": _as_int(row.get(_EQUIP_CURRENT)),
                    "alive": _as_int(row.get(_LIFE_STATE)) == _ALIVE,
                    "team_score": _as_int(row.get(_TEAM_SCORE)),
                    "round_start_time": _as_float(row.get(_ROUND_START_TIME)),
                }
            )
        return dict(by_tick)

    @staticmethod
    def _tick_rate(
        by_tick: dict[int, list[dict[str, Any]]], freeze_ticks: list[int]
    ) -> tuple[float, bool]:
        """Laske tickrate kierrosten alkuaikojen ja tickien suhteesta.

        ``m_fRoundStartTime`` on pelin sekuntikello, tick demon oma laskuri.
        Kahden kierroksen välinen suhde antaa tickraten suoraan; mediaani
        suojaa yksittäiseltä uudelleenkäynnistykseltä. Demon otsikossa
        tickratea ei ole.

        Returns:
            ``(tickrate, mitattiinko)``. Jos mittausta ei saatu tai tulos on
            järkevyysrajojen ulkopuolella, palautetaan
            :data:`DEFAULT_TICK_RATE` ja ``False`` -- vaihe kertoo sen
            käyttäjälle, jottei oletus mene läpi mittauksena.
        """
        havainnot: list[float] = []
        edellinen: tuple[int, float] | None = None
        for tick in freeze_ticks:
            rivit = by_tick.get(tick) or []
            aika = next(
                (
                    r["round_start_time"]
                    for r in rivit
                    if r["round_start_time"] is not None
                ),
                None,
            )
            if aika is None:
                continue
            if edellinen is not None:
                d_tick = tick - edellinen[0]
                d_aika = aika - edellinen[1]
                if d_tick > 0 and d_aika > 0:
                    havainnot.append(d_tick / d_aika)
            edellinen = (tick, aika)
        if not havainnot:
            return DEFAULT_TICK_RATE, False
        rate = statistics.median(havainnot)
        if not TICK_RATE_MIN <= rate <= TICK_RATE_MAX:
            return DEFAULT_TICK_RATE, False
        pyoristetty = round(rate)
        siisti = float(pyoristetty) if abs(rate - pyoristetty) < 0.05 else float(rate)
        return siisti, True

    @staticmethod
    def _lineup_keys(lineups: list[_Lineup]) -> list[str]:
        """Kokoonpanojen tunnisteet; ne eivät saa olla samat.

        Sama tunniste tarkoittaisi, ettei joukkueita voi erottaa toisistaan --
        ja silloin jokainen joukkuekohtainen luku olisi molempien summa.
        """
        avaimet = [lineup.key() for lineup in lineups]
        if avaimet[0] == avaimet[1]:
            raise ParseError(
                "Molemmille joukkueille tuli sama kokoonpanotunniste, joten "
                "niitä ei voi erottaa toisistaan.\n"
                "Kierrosrajojen tickeissä näkyy sama pelaajajoukko molemmilla "
                "puolilla. Demo on todennäköisesti vioittunut."
            )
        return avaimet

    def _build_frame(
        self,
        segments: list[_Segment],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        sivut: list[tuple[str, str]],
        avaimet: list[str],
    ) -> pl.DataFrame:
        anchor_score = [
            _total_score(by_tick.get(s.freeze_end_tick or -1) or []) for s in segments
        ]
        end_score = [_total_score(by_tick.get(s.end_tick or -1) or []) for s in segments]

        # Edellisen kierroksen eloonjääneiden varustearvo, joukkueittain.
        edellinen_saasto: list[int | None] = [None, None]
        rivit: list[dict[str, Any]] = []

        for index, segment in enumerate(segments):
            freeze_rivit = by_tick.get(segment.freeze_end_tick or -1) or []
            end_rivit = by_tick.get(segment.end_tick or -1) or []

            score_start = anchor_score[index]
            if score_start is None and index > 0:
                score_start = end_score[index - 1]
            if score_start is None:
                score_start = end_score[index]

            score_end = anchor_score[index + 1] if index + 1 < len(segments) else None
            if score_end is None:
                score_end = end_score[index]

            saasto_nyt: list[int | None] = [None, None]
            for tiimi, side in enumerate(sivut[index]):
                omat_freeze = _readable(
                    [r for r in freeze_rivit if r["side"] == side]
                )
                omat_end = [r for r in end_rivit if r["side"] == side]
                elossa = [r for r in omat_end if r["alive"]]
                saasto_nyt[tiimi] = (
                    _sum_or_zero([r["equip_current"] for r in elossa])
                    if omat_end
                    else None
                )
                rivit.append(
                    {
                        "round_raw": segment.round_raw,
                        "round_no": None,
                        "lineup_key": avaimet[tiimi],
                        "side": side,
                        "won": (
                            None
                            if segment.winner_side is None
                            else segment.winner_side == side
                        ),
                        "win_reason": segment.win_reason,
                        "money_freeze_end": _sum_or_none(
                            [r["account"] for r in omat_freeze]
                        ),
                        "money_spent": _sum_or_none(
                            [r["cash_spent"] for r in omat_freeze]
                        ),
                        "equip_freeze_end": _sum_or_none(
                            [r["equip_freeze_end"] for r in omat_freeze]
                        ),
                        "equip_round_start": _sum_or_none(
                            [r["equip_round_start"] for r in omat_freeze]
                        ),
                        # Kynnykset ovat per pelaaja, joten jakaja on
                        # havaittava eikä oletettava: vajaalla pelaava
                        # joukkue näyttäisi viidellä jaettuna ecolta.
                        # Jakaja on sama joukko kuin summissa (ks. _readable).
                        "players_freeze_end": len(omat_freeze) or None,
                        "survivors": len(elossa) if omat_end else None,
                        "survivors_equip_prev": edellinen_saasto[tiimi],
                        "freeze_end_tick": segment.freeze_end_tick,
                        "tick_rate": tick_rate,
                        "status": (
                            "ok"
                            if segment.freeze_end_tick is not None
                            else "no_freeze_end"
                        ),
                        "score_start": score_start,
                        "score_end": score_end,
                    }
                )
            edellinen_saasto = saasto_nyt

        return self._typed_frame(rivit)

    @staticmethod
    def _assign_sides(
        segments: list[_Segment],
        by_tick: dict[int, list[dict[str, Any]]],
        lineups: list[_Lineup],
    ) -> list[tuple[str, str]]:
        """Päätä kummalla puolella kumpikin kokoonpano on kullakin kierroksella.

        Joukkueet vaihtavat puolta puoliajalla ja jatkoajassa, joten puoli ei
        kelpaa joukkueen tunnisteeksi. Kokoonpanot tunnistetaan pelaajajoukkojen
        päällekkäisyydestä: se kestää sekä puolenvaihdon että yksittäisen
        pelaajavaihdon.

        Tasapeliä **ei ratkaista arvaamalla**. Jos kumpikaan kuvaus ei voita,
        käytetään edellisen kierroksen kuvausta; jos edellistäkään ei ole,
        parsinta keskeytetään. Hiljainen oletus kohdistaisi voitot väärälle
        joukkueelle.

        Returns:
            Kierroksittain pari ``(kokoonpanon 0 puoli, kokoonpanon 1 puoli)``.
        """
        tulos: list[tuple[str, str]] = []
        edellinen: tuple[str, str] | None = None

        for segment in segments:
            rivit = (
                by_tick.get(segment.freeze_end_tick or -1)
                or by_tick.get(segment.end_tick or -1)
                or []
            )
            joukot = {
                side: {r["steamid"] for r in rivit if r["side"] == side}
                for side in ("T", "CT")
            }
            if not joukot["T"] and not joukot["CT"]:
                tulos.append(_vaadi_edellinen(edellinen, segment, "ei pelaajia"))
                continue

            if not lineups[0].members and not lineups[1].members:
                if not joukot["T"] or not joukot["CT"]:
                    raise ParseError(
                        "Ensimmäiseltä tunnistetulta kierrokselta löytyi "
                        "pelaajia vain toiselta puolelta, joten kokoonpanoja ei "
                        "voi erottaa.\n"
                        "Demo on todennäköisesti katkennut alusta."
                    )
                lineups[0].members |= joukot["T"]
                lineups[1].members |= joukot["CT"]
                edellinen = ("T", "CT")
                tulos.append(edellinen)
                continue

            suora = sum(
                len(joukot[side] & lineups[i].members)
                for i, side in enumerate(("T", "CT"))
            )
            vaihdettu = sum(
                len(joukot[side] & lineups[i].members)
                for i, side in enumerate(("CT", "T"))
            )
            if suora == vaihdettu:
                sivut = _vaadi_edellinen(
                    edellinen, segment, "kokoonpanot eivät erotu toisistaan"
                )
            else:
                sivut = ("T", "CT") if suora > vaihdettu else ("CT", "T")
            for i, side in enumerate(sivut):
                lineups[i].members |= joukot[side]
            edellinen = sivut
            tulos.append(sivut)
        return tulos

    @staticmethod
    def _typed_frame(rivit: list[dict[str, Any]]) -> pl.DataFrame:
        """Rakenna taulu sopimuksen tyypeillä.

        Tyypit annetaan eksplisiittisesti, koska pelkistä null-arvoista Polars
        päättelisi ``Null``-tyypin ja ``schemas.validate`` hylkäisi taulun.
        ``score_start`` ja ``score_end`` ovat osa porttisopimusta
        (``ROUNDS_ADAPTER_COLUMNS``); ``stages.parse`` pudottaa ne ennen
        kirjoitusta.
        """
        schema: dict[str, Any] = {
            name: ROUNDS.get(name, pl.Int32) for name in ROUNDS_ADAPTER_COLUMNS
        }
        if not rivit:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rivit, schema=schema, orient="row")

    # -- Näytepisteet --------------------------------------------------------

    def _sample_points(
        self,
        parser: Any,
        alkuperainen: Path,
        segments: list[_Segment],
        sivut: list[tuple[str, str]],
        lineups: list[_Lineup],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        sample_seconds: tuple[float, ...],
    ) -> tuple[list[SamplePoint], int]:
        """Valitse hetket, joilta pelaajien sijainnit luetaan.

        Aikapisteet tulevat suoraan :func:`~pappascout.domain.sampling.sample_ticks`
        -funktiolta. Ensikontakti ratkaistaan kierros kerrallaan, koska sen
        sääntö vaatii tiedon siitä, kummalla puolella kumpikin pelaaja oli
        **tällä** kierroksella -- puolet vaihtuvat puoliajalla.

        Returns:
            ``(näytepisteet, tuntemattoman puolen takia ohitetut tapahtumat)``.
        """
        bounds = [
            RoundBounds(
                round_raw=s.round_raw,
                freeze_end_tick=s.freeze_end_tick,
                end_tick=s.end_tick,
            )
            for s in segments
        ]
        points = sample_ticks(bounds, tick_rate, sample_seconds)

        hurt = self._damage_events(parser, "player_hurt", alkuperainen)
        deaths = (
            self._damage_events(parser, "player_death", alkuperainen)
            if self.fallback_death
            else []
        )
        if not hurt and not deaths:
            return _sorted_points(points), 0

        lineup_of = _lineup_index_by_player(lineups)
        tuntemattomat = 0
        for index, rajat in enumerate(bounds):
            if not rajat.is_samplable:
                continue
            puolet = _side_lookup(lineup_of, sivut[index], segments[index], by_tick)
            omat_hurt, a = _with_sides(hurt, rajat, puolet)
            omat_deaths, b = _with_sides(deaths, rajat, puolet)
            tuntemattomat += a + b
            tick = first_contact_tick(
                omat_hurt,
                rajat,
                exclude_weapons=self.exclude_weapons,
                death_events=omat_deaths,
                fallback_death=self.fallback_death,
            )
            if tick is None:
                continue
            assert rajat.freeze_end_tick is not None  # is_samplable
            t_s = seconds_since_freeze_end(tick, rajat.freeze_end_tick, tick_rate)
            points.append(
                SamplePoint(
                    round_raw=rajat.round_raw,
                    tick=tick,
                    sample_kind=FIRST_CONTACT_SAMPLE,
                    sample_t_s=t_s,
                    t_s=t_s,
                )
            )
        return _sorted_points(points), tuntemattomat

    def _damage_events(
        self, parser: Any, name: str, alkuperainen: Path
    ) -> list[tuple[int, str | None, str | None, str | None]]:
        """Lue ``player_hurt``- tai ``player_death``-tapahtumat.

        Puolia ei liitetä tässä: sama pelaaja on eri puolella ennen ja jälkeen
        puoliajan, joten kuvaus on kierroskohtainen.

        Returns:
            ``(tick, attacker_id, victim_id, weapon)``. Puuttuva tapahtuma ei
            ole virhe -- kierros voi ratketa ilman yhtään vahinkoa.
        """
        frame = self._event(parser, name, alkuperainen)
        if frame is None:
            # Tapahtumaa ei ole demossa lainkaan. Se on mahdollista (kierros
            # voi ratketa ilman vahinkoa), joten se ei ole virhe.
            return []

        puuttuvat = [
            sarake for sarake in DAMAGE_COLUMNS if sarake not in frame.columns
        ]
        if puuttuvat:
            raise ParseError(
                f"Demon {alkuperainen.name} tapahtumasta {name!r} puuttuu "
                f"sarake: {', '.join(puuttuvat)}.\n"
                "Ilman sitä jokainen tapahtuma hylättäisiin äänettömästi ja "
                "tulos väittäisi, ettei yhdelläkään kierroksella ollut "
                "ensikontaktia. Kenttä on todennäköisesti nimetty uudelleen "
                "demoparser2:n päivityksessä -- päivitä "
                "adapters/demo_parser.py:n DAMAGE_COLUMNS."
            )

        rivit: list[tuple[int, str | None, str | None, str | None]] = []
        for row in frame.to_dict("records"):
            tick = _as_int(row.get("tick"))
            if tick is None:
                continue
            rivit.append(
                (
                    tick,
                    _as_str(row.get("attacker_steamid")),
                    _as_str(row.get("user_steamid")),
                    _as_str(row.get("weapon")),
                )
            )
        return rivit

    def _build_ticks_frame(
        self,
        points: list[SamplePoint],
        parser: Any,
        alkuperainen: Path,
        segments: list[_Segment],
        sivut: list[tuple[str, str]],
        avaimet: list[str],
    ) -> tuple[pl.DataFrame, int]:
        """Lue pelaajien sijainnit näytepisteiden tickeiltä ja rakenna taulu.

        Rivi syntyy **jokaisesta** pelaajasta, myös kuolleesta: kuolleiden
        suodatus on aggregoinnin työ (AD-10), ei parsinnan. Tuntematon alue
        jää ``null``:ksi, mutta koordinaatit tallentuvat silti -- riviä ei
        pudoteta hiljaa.

        Returns:
            ``(taulu, vajaiden näytepisteiden määrä)``. Vajaa näytepiste on
            sellainen, jolta saatiin vähemmän pelaajia kuin demon parhaalta
            pisteeltä. Luku raportoidaan, koska systemaattinen propivika
            näkyisi muuten vasta vinoutuneina aggregaatteina.
        """
        if not points:
            return self._typed_ticks_frame([]), 0

        wanted = sorted({p.tick for p in points})
        by_tick = self._read_sample_ticks(parser, wanted, alkuperainen)
        # sivut on segmenttien järjestyksessä, mutta näytepiste tuntee vain
        # round_raw-arvon, joten kuvaus tarvitaan takaisin segmentti-indeksiin.
        index_by_raw = {s.round_raw: index for index, s in enumerate(segments)}
        avain_puolelle = [
            _keys_by_side(sivut[index], avaimet, segment)
            for index, segment in enumerate(segments)
        ]

        rivit: list[dict[str, Any]] = []
        pelaajia_pisteella: list[int] = []
        for point in points:
            segment_index = index_by_raw.get(point.round_raw)
            if segment_index is None:  # pragma: no cover - sample_ticks takaa
                continue
            avaimet_side = avain_puolelle[segment_index]
            tickin_rivit = by_tick.get(point.tick, ())
            if not tickin_rivit:
                raise ParseError(
                    f"Demon {alkuperainen.name} naytepisteeltä "
                    f"(round_raw={point.round_raw}, {point.sample_kind}, "
                    f"t={point.sample_t_s:g} s, tick={point.tick}) ei saatu "
                    "yhtään pelaajariviä.\n"
                    "Tick on kierroksen rajojen sisällä, joten tyhjä tulos "
                    "tarkoittaa että demo on vioittunut tai demoparser2 ei "
                    "palauta tältä tickiltä mitään. Näytepiste laskettaisiin "
                    "mukaan lukuihin mutta puuttuisi taulusta."
                )
            pelaajia_pisteella.append(len(tickin_rivit))
            for rivi in tickin_rivit:
                side = rivi["side"]
                rivit.append(
                    {
                        "round_raw": point.round_raw,
                        "round_no": None,
                        "player_id": rivi["steamid"],
                        "lineup_key": avaimet_side[side],
                        "side": side,
                        "sample_kind": point.sample_kind,
                        "sample_t_s": point.sample_t_s,
                        "t_s": point.t_s,
                        "x": rivi["x"],
                        "y": rivi["y"],
                        "z": rivi["z"],
                        "area": rivi["area"],
                        "is_alive": rivi["alive"],
                    }
                )

        # Odotettu pelaajamäärä luetaan demosta itsestään: [thresholds] ei näy
        # tähän vaiheeseen (AD-3), joten roster_size'a ei voi käyttää.
        täysi = max(pelaajia_pisteella, default=0)
        vajaat = sum(1 for määrä in pelaajia_pisteella if määrä < täysi)
        return self._typed_ticks_frame(rivit), vajaat

    def _read_sample_ticks(
        self, parser: Any, ticks: list[int], alkuperainen: Path
    ) -> dict[int, list[dict[str, Any]]]:
        """Lue sijaintipropit annetuilta tickeiltä ja ryhmittele tickin mukaan."""
        if not ticks:
            return {}
        try:
            frame = parser.parse_ticks(list(SAMPLE_TICK_PROPS), ticks=ticks)
        except Exception as exc:  # noqa: BLE001 - kirjaston oma virhetyyppi
            raise ParseError(
                f"Demon {alkuperainen.name} näytepisteitä ei voitu lukea: {exc}\n"
                "Tiedosto on todennäköisesti vioittunut tai demoparser2:n "
                "versio ei tunne näitä kenttiä. Aja: uv sync"
            ) from exc

        saadut = set(getattr(frame, "columns", ()))
        puuttuvat = [
            name
            for name in (*SAMPLE_TICK_PROPS, "tick", "steamid")
            if name not in saadut
        ]
        if puuttuvat:
            raise ParseError(
                "demoparser2 ei palauttanut kaikkia näytepisteen kenttiä "
                f"demosta {alkuperainen.name}. Puuttuu: {', '.join(puuttuvat)}.\n"
                "Kenttä on todennäköisesti nimetty uudelleen demoparser2:n "
                "päivityksessä. Ilman tarkistusta asetelmataulu näyttäisi "
                "kelvolliselta mutta olisi tyhjä tai paikaton. Päivitä "
                "adapters/demo_parser.py:n propinimet."
            )

        by_tick: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in frame.to_dict("records"):
            steamid = _as_str(row.get("steamid"))
            side = TEAM_SIDES.get(_as_int(row.get(_TEAM_NUM)) or -1)
            tick = _as_int(row.get("tick"))
            if steamid is None or side is None or tick is None:
                # Katsojat ja liittymättömät eivät ole kierroksen osapuolia.
                continue
            life_state = _as_int(row.get(_LIFE_STATE))
            if life_state is None:
                # is_alive ei ole nullable, joten puuttuva arvo muuttuisi
                # hiljaa arvoksi False ja elossa oleva pelaaja katoaisi
                # aggregoinnista. Tuntematon alue saa jäädä nulliksi, mutta
                # tämä ei voi.
                raise ParseError(
                    f"Demon {alkuperainen.name} tickistä {tick} puuttuu "
                    f"pelaajan {steamid} {_LIFE_STATE}.\n"
                    "Elossaolo on pakollinen havainto: puuttuvasta arvosta "
                    "tulisi 'kuollut', ja pelaaja katoaisi asetelmasta "
                    "äänettömästi. Tarkista demoparser2:n versio."
                )
            by_tick[tick].append(
                {
                    "steamid": steamid,
                    "side": side,
                    # Tyhjä aluenimi on pelin tapa sanoa "ei nimettyä aluetta".
                    # Se säilyy null:na; koordinaatit kertovat silti paikan.
                    "area": _as_str(row.get(_PLACE_NAME)),
                    "x": _as_float(row.get(_X)),
                    "y": _as_float(row.get(_Y)),
                    "z": _as_float(row.get(_Z)),
                    "alive": life_state == _ALIVE,
                }
            )
        return dict(by_tick)

    @staticmethod
    def _typed_ticks_frame(rivit: list[dict[str, Any]]) -> pl.DataFrame:
        """Rakenna näytepistetaulu sopimuksen tyypeillä.

        Tyypit annetaan eksplisiittisesti samasta syystä kuin kierrostaulussa:
        pelkistä null-arvoista Polars päättelisi ``Null``-tyypin.
        """
        schema: dict[str, Any] = {name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS}
        if not rivit:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rivit, schema=schema, orient="row")


def _keys_by_side(
    sivut: tuple[str, str], avaimet: list[str], segment: _Segment
) -> dict[str, str]:
    """Puoli -> kokoonpanotunniste yhdellä kierroksella.

    Sanakirja eikä ``sivut.index(side)``: jos puolikuvaus olisi jostain syystä
    ``("T", "T")``, ``.index`` palauttaisi molemmille nollan ja **molemmat
    joukkueet saisivat saman lineup_keyn**. Taulu näyttäisi kelvolliselta,
    mutta jokainen joukkuekohtainen luku olisi molempien summa -- täsmälleen se
    ristiinkytkentä, jonka :meth:`Demoparser2Adapter._lineup_keys` estää
    kierrostaulussa.
    """
    if sivut[0] == sivut[1]:
        raise ParseError(
            f"Kierroksella (round_raw={segment.round_raw}, "
            f"freeze_end_tick={segment.freeze_end_tick}) molemmille "
            f"kokoonpanoille tuli sama puoli {sivut[0]!r}.\n"
            "Puolet eivät erotu, joten näytepisteiden rivit kohdistuisivat "
            "samalle joukkueelle. Demo on todennäköisesti vioittunut."
        )
    return {sivut[0]: avaimet[0], sivut[1]: avaimet[1]}


def _lineup_index_by_player(lineups: list[_Lineup]) -> dict[str, int]:
    """Pelaaja -> kokoonpanon indeksi.

    Pelaaja, joka on ehtinyt näkyä molemmissa kokoonpanoissa, jätetään pois:
    hänen puoltaan ei voi päätellä, ja arvaus kohdistaisi kontaktin väärin
    päin. Sellaista ei normaalissa demossa esiinny.
    """
    tulos: dict[str, int] = {}
    molemmissa = lineups[0].members & lineups[1].members
    for index, lineup in enumerate(lineups):
        for steamid in lineup.members - molemmissa:
            tulos[steamid] = index
    return tulos


def _side_lookup(
    lineup_of: dict[str, int],
    sivut: tuple[str, str],
    segment: _Segment,
    by_tick: dict[int, list[dict[str, Any]]],
) -> dict[str, str]:
    """Pelaaja -> puoli **tällä kierroksella**.

    Ensisijainen lähde on kokoonpano: puoli tulee kierroksen omasta
    kuvauksesta, ei pelaajasta, koska joukkueet vaihtavat puolta puoliajalla ja
    jatkoajassa.

    Varalähteenä on kierroksen oman tickin ``m_iTeamNum``. Sitä tarvitaan
    pelaajalle, joka ei ole kummassakaan kokoonpanossa -- kesken karttaa tullut
    tai uudelleenyhdistänyt pelaaja. Ilman varalähdetta hanen vahinkonsa
    hylättäisiin äänettömästi ja kierros voisi menettaa ensikontaktinsa.
    """
    puolet = {steamid: sivut[index] for steamid, index in lineup_of.items()}
    for tick in (segment.freeze_end_tick, segment.end_tick):
        for rivi in by_tick.get(tick or -1) or ():
            puolet.setdefault(rivi["steamid"], rivi["side"])
    return puolet


def _with_sides(
    events: list[tuple[int, str | None, str | None, str | None]],
    bounds: RoundBounds,
    puolet: dict[str, str],
) -> tuple[list[DamageEvent], int]:
    """Rajaa tapahtumat kierrokseen ja liitä niihin pelaajien puolet.

    Returns:
        ``(tapahtumat, montako jäi ilman puolta)``. Jälkimmäinen luku päätyy
        diagnostiikkaan: äänettömästi hylätty vahinko voisi viedä kierrokselta
        ensikontaktin, eikä mikään kertoisi siitä.
    """
    if bounds.freeze_end_tick is None or bounds.end_tick is None:
        return [], 0
    alku, loppu = bounds.freeze_end_tick, bounds.end_tick

    tulos: list[DamageEvent] = []
    tuntemattomat = 0
    for tick, attacker, victim, weapon in events:
        if not alku <= tick <= loppu:
            continue
        attacker_side = puolet.get(attacker) if attacker else None
        victim_side = puolet.get(victim) if victim else None
        # Maailman aiheuttama vahinko (attacker None) on tunnettu tapaus eikä
        # puuttuva havainto, joten sitä ei lasketa tuntemattomaksi.
        if (attacker and attacker_side is None) or (victim and victim_side is None):
            tuntemattomat += 1
        tulos.append(
            DamageEvent(
                tick=tick,
                attacker_id=attacker,
                victim_id=victim,
                weapon=weapon,
                attacker_side=attacker_side,
                victim_side=victim_side,
            )
        )
    return tulos, tuntemattomat


def _sorted_points(points: list[SamplePoint]) -> list[SamplePoint]:
    """Näytepisteet vakaassa järjestyksessä.

    ``sample_kind`` on avaimessa, koska ensikontakti voi osua tasan
    konfiguroidulle sekunnille. Ilman sitä kahden rivin järjestys riippuisi
    syötejärjestyksestä, ja sama demo tuottaisi eri tavut eri ajoilla.
    """
    return sorted(points, key=lambda p: (p.round_raw, p.sample_t_s, p.sample_kind))


def _vaadi_edellinen(
    edellinen: tuple[str, str] | None, segment: _Segment, syy: str
) -> tuple[str, str]:
    """Palauta edellisen kierroksen puolikuvaus tai keskeytä.

    Oletus ``("T", "CT")`` olisi arvaus, joka näyttäisi toimivan mutta
    kohdistaisi kierroksen havainnot väärälle joukkueelle.
    """
    if edellinen is not None:
        return edellinen
    raise ParseError(
        f"Kierroksen (freeze_end_tick={segment.freeze_end_tick}, "
        f"round_end_tick={segment.end_tick}) puolia ei voitu määrittää: {syy}, "
        "eikä edellistä kierrosta ole, josta kuvauksen voisi periä.\n"
        "Puolen arvaaminen kohdistaisi kierroksen havainnot väärälle "
        "joukkueelle, joten parsinta keskeytetään. Demo on todennäköisesti "
        "vioittunut."
    )


# -- Pieniä muuntimia ---------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:  # pragma: no cover - vertailukelvoton tyyppi
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:  # pragma: no cover
        return None
    text = str(value).strip()
    return text or None


def _as_side(value: Any) -> str | None:
    text = _as_str(value)
    if text is None:
        return None
    text = text.upper()
    return text if text in ("T", "CT") else None


#: Propit, joiden on oltava luettavissa, jotta pelaaja lasketaan mukaan
#: freezetimen lopun summiin ja niiden jakajaan.
_FREEZE_END_PROPS: tuple[str, ...] = (
    "account",
    "cash_spent",
    "equip_freeze_end",
    "equip_round_start",
)


def _readable(rivit: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pelaajat, joiden freezetimen lopun arvot ovat kaikki luettavissa.

    Sekä summa että sen jakaja lasketaan **tästä samasta joukosta**. Jos
    summattaisiin vain luettavat mutta jaettaisiin kaikilla riveillä, kolmen
    pelaajan varustearvo jaettuna viidellä aliarvioisi tuloksen 40 prosenttia
    ja työntäisi kierroksen ecoksi -- hiljaa ja uskottavan näköisesti.
    """
    return [
        r for r in rivit if all(r.get(name) is not None for name in _FREEZE_END_PROPS)
    ]


def _sum_or_none(values: list[int | None]) -> int | None:
    """Summaa arvot; ``None`` jos yhtään havaintoa ei ole."""
    kelvolliset = [v for v in values if v is not None]
    return sum(kelvolliset) if kelvolliset else None


def _sum_or_zero(values: list[int | None]) -> int:
    """Summaa arvot; tyhjä joukko on nolla (kukaan ei jäänyt henkiin)."""
    return sum(v for v in values if v is not None)


def _total_score(rivit: list[dict[str, Any]]) -> int | None:
    """Joukkueiden yhteispistemäärä yhdessä tickissä.

    Summa kestää puoliajan vaihdon: joukkuekohtaiset pisteet vaihtavat paikkaa,
    mutta summa säilyy ja kasvaa vain pelatusta kierroksesta.

    Vaatii **molempien** puolten lukeman. Yksipuolinen summa näyttäisi
    kelvolliselta luvulta mutta olisi liian pieni, jolloin kierros voisi pudota
    pelattujen joukosta -- tai pysyä mukana väärällä numerolla.
    """
    per_side: dict[str, int] = {}
    for rivi in rivit:
        if rivi["team_score"] is not None:
            per_side.setdefault(rivi["side"], rivi["team_score"])
    if len(per_side) != len(TEAM_SIDES):
        return None
    return sum(per_side.values())

