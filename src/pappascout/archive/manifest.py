"""Manifest: the skip contract between stages (AD-1).

Every stage result gets a ``*.manifest.json`` file beside it. When a stage is
about to run again, it builds the manifest it expects and compares it with the
one on disk: if they match, the stage is skipped. This is the mechanism that
lets a threshold adjustment finish in seconds -- parsing is not run again,
because the ``parse`` manifest's ``params_hash`` is computed from the
``[parse]`` section alone and does not change.

Input digests are **not recomputed from the files**; they are read from the
inputs' own meta or manifest files. Hashing one demo is 233 MB of work, and
in the synchronised folder that is both slow and prone to false
invalidations. The mechanism behind both: the sync client is free to release
a file's local copy while keeping the file itself, so hashing it drags all
233 MB back down again, and a file still being transferred hashes to
whatever has arrived so far -- a digest that does not match, on an input
nobody changed, which reruns the stage for nothing.

The rule for the ``tool_versions`` field
----------------------------------------
The manifest records **only those tools whose version really changes the
result of that stage** -- not every installed package, and not pappascout's
own version. The reason: if the ``pappascout`` version were included, every
patch release would invalidate the whole archive and force hundreds of demos
to be parsed again. That runs straight against the promise of a fast re-run.

===============  =============================================
Stage            ``tool_versions``
===============  =============================================
``parse``        ``demoparser2``
``classify``     (empty -- pure domain computation)
``aggregate``    (empty)
``render``       ``jinja2``, if the report template changes
===============  =============================================

Versions are read from the installed packages with :func:`tool_versions`, not
written by hand.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pappascout.archive.atomic_write import atomic_write_text
from pappascout.constants import UnitStatus
from pappascout.errors import PappascoutError

__all__ = [
    "ManifestInput",
    "Manifest",
    "compute_params_hash",
    "tool_versions",
    "MANIFEST_SCHEMA_VERSION",
]

MANIFEST_SCHEMA_VERSION = "1.0.0"


class ManifestInput(BaseModel):
    """One input to a stage: the previous result's id and its digest.

    ``sha256`` is read from the input's own meta or manifest file, not
    computed from the file again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str
    sha256: str

    def key(self) -> tuple[str, str]:
        return (self.result_id, self.sha256)


class Manifest(BaseModel):
    """A stage result's manifest.

    Attributes:
        result_id: This result's id, which the next stage records as its
            input.
        stage: Name of the stage, for example ``"parse"``.
        inputs: The inputs with their ids and digests.
        params_hash: Digest of **only** the settings section the stage reads.
        tool_versions: Tool versions this stage's result depends on. See the
            rule in the module docstring -- not pappascout's own version.
        created_at: Creation time in UTC.
        status: Unit status (AD-9). Only ``ok`` qualifies for a skip.
        reason: Free-form explanation for a status other than ``ok``.
        outputs: The result's files as archive-internal relative paths.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = MANIFEST_SCHEMA_VERSION
    result_id: str
    stage: str
    inputs: list[ManifestInput] = Field(default_factory=list)
    params_hash: str
    tool_versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    status: UnitStatus = "ok"
    reason: str | None = None
    outputs: list[str] = Field(default_factory=list)

    @classmethod
    def new(
        cls,
        *,
        result_id: str,
        stage: str,
        params_hash: str,
        inputs: Sequence[ManifestInput] = (),
        tool_versions: Mapping[str, str] | None = None,
        status: UnitStatus = "ok",
        reason: str | None = None,
        outputs: Sequence[str] = (),
    ) -> Manifest:
        """Build a manifest with the current timestamp."""
        return cls(
            result_id=result_id,
            stage=stage,
            params_hash=params_hash,
            inputs=list(inputs),
            tool_versions=dict(tool_versions or {}),
            created_at=datetime.now(UTC),
            status=status,
            reason=reason,
            outputs=[str(path) for path in outputs],
        )

    def is_current(
        self,
        *,
        inputs: Sequence[ManifestInput],
        params_hash: str,
        tool_versions: Mapping[str, str],
        root: Path | str,
    ) -> bool:
        """Tell whether the stage may be skipped.

        A skip requires that

        * the manifest's ``schema_version`` is one this code knows,
        * the status is ``ok``,
        * inputs, params hash and tool versions are exactly the same, and
        * **every ``outputs`` file is still on disk**.

        The last condition is required by the synchronised folder: a small
        manifest syncs quickly, but a result of hundreds of megabytes may
        still be on its way, or the user may have deleted it. Without the
        check the stage would be skipped and the next stage would fail on a
        missing file.

        The order of the inputs does not matter. The timestamp is ignored --
        otherwise nothing would ever be skipped.

        Args:
            inputs: The expected inputs with their ids and digests.
            params_hash: The expected params hash.
            tool_versions: The expected tool versions.
            root: Archive root the ``outputs`` paths are resolved against.
        """
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            return False
        if self.status != "ok":
            return False
        if self.params_hash != params_hash:
            return False
        if dict(self.tool_versions) != dict(tool_versions):
            return False
        if sorted(i.key() for i in self.inputs) != sorted(i.key() for i in inputs):
            return False
        return self.outputs_present(root)

    def outputs_present(self, root: Path | str) -> bool:
        """Are all of the result's files still on disk?"""
        return not self.missing_outputs(root)

    def missing_outputs(self, root: Path | str) -> list[str]:
        """The missing output files -- for logging and error messages."""
        base = Path(root)
        return [name for name in self.outputs if not (base / Path(name)).exists()]

    def fingerprint(self) -> str:
        """This result's id, computed **from the manifest's contents**.

        The next stage writes the value into its own
        :attr:`ManifestInput.sha256` field. **It is not a file digest** but
        the sha256 of this manifest's params hash, inputs, tool versions,
        output files and status. The stage's result tables are not hashed --
        they are derived, and their identity is exactly what they were
        derived from.

        The creation time is left out deliberately: the same input with the
        same settings produces the same result, and a bare re-run
        (``--pakota``) must not force the next stage to run again. Everything
        else is included, so a changed demo, a changed setting or a changed
        tool version shows up immediately.

        One definition, because both ``classify`` and ``aggregate`` identify
        their inputs this way -- two copies would diverge sooner or later, and
        then one stage would skip the work the other would run again.
        """
        return compute_params_hash(
            {
                "params_hash": self.params_hash,
                "inputs": sorted([i.result_id, i.sha256] for i in self.inputs),
                "tool_versions": dict(self.tool_versions),
                "outputs": sorted(self.outputs),
                "status": self.status,
            }
        )

    def write(self, path: Path | str) -> Path:
        """Write the manifest atomically as JSON."""
        text = self.model_dump_json(indent=2)
        return atomic_write_text(path, text + "\n")

    @classmethod
    def read(cls, path: Path | str) -> Manifest:
        """Read a manifest from disk.

        Raises:
            PappascoutError: If the file does not exist, is corrupt, or was
                written with an unknown schema version.
        """
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise PappascoutError(
                f"No manifest was found at {path}. "
                "Run the stage again and the manifest will be created."
            ) from exc
        try:
            manifest = cls.model_validate_json(raw)
        except ValidationError as exc:
            raise PappascoutError(
                f"Manifest {path} is corrupt and cannot be read. "
                "Delete the file and run the stage again.\n"
                f"{exc}"
            ) from exc

        if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
            raise PappascoutError(
                f"Manifest {path} was written by a newer version "
                f"(the manifest says {manifest.schema_version}, this code "
                f"knows version {MANIFEST_SCHEMA_VERSION}).\n"
                "Update pappascout to the latest version, or delete the "
                "manifest so the stage is run again."
            )
        return manifest

    @classmethod
    def read_if_exists(cls, path: Path | str) -> Manifest | None:
        """Read a manifest, or return ``None`` if there is none.

        A corrupt manifest, or one written with a foreign schema version, is
        treated as missing: the stage is run again instead of the run failing.
        """
        path = Path(path)
        if not path.is_file():
            return None
        try:
            return cls.read(path)
        except PappascoutError:
            return None


def compute_params_hash(params: Mapping[str, Any]) -> str:
    """Compute the params hash for a single settings section.

    The hash is computed from a canonical JSON representation, so that key
    order or TOML formatting does not change the result. Pass **only** the
    settings section the stage really reads -- that is the condition the whole
    skip mechanism rests on.

    Every value has to be a JSON type. Serialisation is not patched up with a
    ``str()`` fallback, because ``WindowsPath`` for instance stringifies
    differently per machine: two machines would get different hashes from the
    same setting and the whole archive would be parsed again.

    Args:
        params: The settings section as a dict, for example
            ``settings.parse.model_dump(mode="json")`` extended with the tool
            version.

    Returns:
        A 64-character hexadecimal sha256 digest.

    Raises:
        PappascoutError: If some value is not JSON-serialisable. The message
            names the key and its type.
    """
    try:
        canonical = json.dumps(
            params, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except TypeError as exc:
        offending = _offending_keys(params)
        raise PappascoutError(
            "The params hash cannot be computed: the settings section holds "
            f"a value that cannot be represented as JSON ({offending or exc}).\n"
            "Convert the value to a JSON type first, for example "
            'settings.parse.model_dump(mode="json").\n'
            "Why this is strict: WindowsPath, for instance, stringifies "
            "differently per machine, so two machines would compute different "
            "hashes and the whole archive would be parsed again."
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _offending_keys(params: Mapping[str, Any]) -> str:
    """Find the keys whose value is not JSON-serialisable."""
    offending: list[str] = []

    def walk(value: Any, path: str) -> None:
        if value is None or isinstance(value, (str, int, float, bool)):
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                walk(item, f"{path}.{key}" if path else str(key))
            return
        if isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")
            return
        offending.append(f"{path} = {type(value).__name__}")

    walk(dict(params), "")
    return ", ".join(offending)


def tool_versions(*names: str) -> dict[str, str]:
    """Read the versions of the named packages from the installation.

    Pass **only those tools whose version changes this stage's result** (see
    the module docstring). For example::

        tool_versions("demoparser2")   # for the parse stage's manifest

    Raises:
        PappascoutError: If a package is not installed. Skipping silently
            would produce a manifest that looks like a match even though the
            tool has changed.
    """
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = _package_version(name)
        except PackageNotFoundError as exc:
            raise PappascoutError(
                f"Package {name} is not installed, so its version cannot be "
                "recorded in the manifest. Run: uv sync"
            ) from exc
    return versions
