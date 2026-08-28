"""Arkistokerros: ainoa pysyvä tila (AD-7).

Kaikki tulokset ovat tiedostoja ``archive_root``in alla. Manifestit ja indeksit
viittaavat vain suhteellisilla poluilla, jotta sama arkisto toimii molemmilla
koneilla. Kaikki kirjoitukset ovat atomisia, koska arkisto on OneDrivessa.

``archive`` ei riipu ``domain``ista -- se on putki, ei domain-mallien säilö.
"""

from pappascout.archive.atomic_write import (
    atomic_path,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    temp_suffix,
)
from pappascout.archive.manifest import (
    Manifest,
    ManifestInput,
    compute_params_hash,
    tool_versions,
)
from pappascout.archive.paths import ARCHIVE_ROOT_ENV_VAR, ArchivePaths, safe_component

__all__ = [
    "ArchivePaths",
    "Manifest",
    "ManifestInput",
    "compute_params_hash",
    "tool_versions",
    "safe_component",
    "ARCHIVE_ROOT_ENV_VAR",
    "atomic_path",
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
    "temp_suffix",
]
