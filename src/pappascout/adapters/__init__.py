"""Adapterikerros: ainoa paikka, joka tuntee ulkomaailman.

Spinen riippuvuusnuoli on ``stages -> adapters -> domain``. Adapteri kääntää
vieraan kirjaston tai rajapinnan käsitteet pappascoutin omiksi tauluiksi, ja
vaihe näkee vain protokollan -- ei demoparser2:ta, ei FACEITia, ei HTTP:tä
(AD-8).

Tässä paketissa on kolme osaa:

``protocols``
    Portit, jotka vaiheet ottavat parametrina. Tuonti on kevyt eikä vedä
    mukanaan raskaita riippuvuuksia, joten testifeikki voi toteuttaa portin
    ilman demoparser2:ta.
``decompress``
    Pakatun demon purku. Erillään parsinnasta, koska Epic 3:n demolataus
    tarvitsee saman purun sellaisenaan.
``demo_parser``
    demoparser2-toteutus. **Ainoa** moduuli, jossa pelin propinimet
    (``CCSPlayerPawn.*``) esiintyvät.

``demo_parser`` tuodaan tarkoituksella vain nimellä (``from
pappascout.adapters.demo_parser import Demoparser2Adapter``): näin pelkkä
``import pappascout.adapters`` ei lataa demoparser2:ta.
"""

from pappascout.adapters.protocols import (
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoParser,
    DemoTables,
    ParseDiagnostics,
)

__all__ = [
    "DemoParser",
    "DemoTables",
    "ParseDiagnostics",
    "ROUNDS_ADAPTER_COLUMNS",
    "TICKS_ADAPTER_COLUMNS",
]
