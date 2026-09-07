"""Archive layer: the only durable state (AD-7).

Every result is a file under ``archive_root``. Manifests and indexes refer to
files by relative path only, so that the same archive works on both machines.
Every write is atomic, because the archive lives in a synchronised folder.

``archive`` does not depend on ``domain`` -- it is plumbing, not a store for
domain models.
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
