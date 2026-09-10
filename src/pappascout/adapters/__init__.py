"""The adapter layer: the only place that knows the outside world.

The spine's dependency arrow is ``stages -> adapters -> domain``. An adapter
translates the concepts of a foreign library or interface into pappascout's
own tables, and a stage sees only the protocol -- not demoparser2, not FACEIT,
not HTTP (AD-8).

This package has four parts:

``protocols``
    The ports the stages take as a parameter. The import is light and does not
    drag heavy dependencies along with it, so a test fake can implement a port
    without demoparser2.
``decompress``
    Decompressing a compressed demo. Kept apart from parsing, because Epic 3's
    demo download needs the same decompression as it stands.
``demo_parser``
    The demoparser2 implementation. The **only** module in which the game's
    prop names (``CCSPlayerPawn.*``) appear.
``faceit``
    The FACEIT Data API implementation. The **only** module that makes HTTP
    calls.

``demo_parser`` is deliberately imported by name only (``from
pappascout.adapters.demo_parser import Demoparser2Adapter``): that way a plain
``import pappascout.adapters`` does not load demoparser2.
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
