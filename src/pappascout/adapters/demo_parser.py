"""demoparser2-toteutus kierrostaululle (AD-8).

**Tämä on ainoa moduuli, jossa pelin propinimet esiintyvät.** Vaihe näkee vain
:class:`~pappascout.adapters.protocols.DemoRoundsParser`-portin, joten
demoparser2:n vaihtaminen tai päivittäminen ei kosketa putkea.

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

Muistinkäyttö
-------------
Demoa ei ladata muistiin kokonaan. ``parse_ticks`` kutsutaan **vain
kierrosrajojen tickeille** (Ancient: 44 tickiä, ~440 riviä), ei koko
tickisarjalle. Pakattu demo puretaan virtaavasti temp-tiedostoon.
"""

from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from pappascout.adapters.decompress import readable_demo
from pappascout.adapters.protocols import ROUNDS_ADAPTER_COLUMNS, ParseDiagnostics
from pappascout.domain.schemas import ROUNDS
from pappascout.errors import ParseError

__all__ = [
    "Demoparser2Rounds",
    "TEAM_SIDES",
    "TICK_PROPS",
    "DEFAULT_TICK_RATE",
    "TICK_RATE_MIN",
    "TICK_RATE_MAX",
]

# -- Pelin kentät -------------------------------------------------------------

_TEAM_NUM = "CCSPlayerController.m_iTeamNum"
_ACCOUNT = "CCSPlayerController.CCSPlayerController_InGameMoneyServices.m_iAccount"
_EQUIP_FREEZE_END = "CCSPlayerPawn.m_unFreezetimeEndEquipmentValue"
_EQUIP_ROUND_START = "CCSPlayerPawn.m_unRoundStartEquipmentValue"
_EQUIP_CURRENT = "CCSPlayerPawn.m_unCurrentEquipmentValue"
_LIFE_STATE = "CCSPlayerPawn.m_lifeState"
_TEAM_SCORE = "CCSTeam.m_iScore"
_ROUND_START_TIME = "CCSGameRulesProxy.CCSGameRules.m_fRoundStartTime"

#: Propit, jotka luetaan kierrosrajojen tickeistä.
TICK_PROPS: tuple[str, ...] = (
    _TEAM_NUM,
    _ACCOUNT,
    _EQUIP_FREEZE_END,
    _EQUIP_ROUND_START,
    _EQUIP_CURRENT,
    _LIFE_STATE,
    _TEAM_SCORE,
    _ROUND_START_TIME,
)

#: ``m_iTeamNum`` -> puoli. 0 ja 1 ovat katsoja ja liittymätön, eivät joukkueita.
TEAM_SIDES: dict[int, str] = {2: "T", 3: "CT"}

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


class Demoparser2Rounds:
    """Lukee kierrostaulun demoparser2:lla.

    Toteuttaa :class:`~pappascout.adapters.protocols.DemoRoundsParser`-portin.

    Attributes:
        diagnostics: Viimeisimmän parsinnan havainnot, jotka eivät mahdu
            ``ROUNDS``-sopimukseen. ``None`` ennen ensimmäistä kutsua.
    """

    def __init__(self) -> None:
        self.diagnostics: ParseDiagnostics | None = None

    def parse_rounds(self, path: Path) -> pl.DataFrame:
        """Ks. portin dokumentaatio."""
        path = Path(path)
        with readable_demo(path) as demo_path:
            return self._parse(demo_path, path)

    # -- Sisäinen ------------------------------------------------------------

    def _parse(self, demo_path: Path, alkuperainen: Path) -> pl.DataFrame:
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
        frame = self._build_frame(segments, by_tick, tick_rate)
        self.diagnostics = ParseDiagnostics(
            tick_rate=tick_rate,
            tick_rate_measured=measured,
            rounds_seen=len(segments),
        )
        return frame

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

        Demoparser2Rounds._assign_round_raw(segments)
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

    def _build_frame(
        self,
        segments: list[_Segment],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
    ) -> pl.DataFrame:
        lineups = [_Lineup(), _Lineup()]
        sivut = self._assign_sides(segments, by_tick, lineups)
        avaimet = [lineup.key() for lineup in lineups]
        if avaimet[0] == avaimet[1]:
            raise ParseError(
                "Molemmille joukkueille tuli sama kokoonpanotunniste, joten "
                "niitä ei voi erottaa toisistaan.\n"
                "Kierrosrajojen tickeissä näkyy sama pelaajajoukko molemmilla "
                "puolilla. Demo on todennäköisesti vioittunut."
            )

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
                omat_freeze = [r for r in freeze_rivit if r["side"] == side]
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
                        "equip_freeze_end": _sum_or_none(
                            [r["equip_freeze_end"] for r in omat_freeze]
                        ),
                        "equip_round_start": _sum_or_none(
                            [r["equip_round_start"] for r in omat_freeze]
                        ),
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

