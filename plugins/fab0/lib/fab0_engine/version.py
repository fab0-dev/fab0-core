"""The release of the plugin this engine belongs to."""

from __future__ import annotations

import json
from pathlib import Path

from .errors import EXIT_ERROR, Fail
from .paths import MANIFEST


def read_version(manifest: Path | None = None) -> str:
    """The release named in the plugin's manifest; reads nothing else."""
    manifest = manifest or MANIFEST
    try:
        data = json.loads(manifest.read_text())
    except OSError as e:
        raise Fail(EXIT_ERROR, f"cannot read the plugin manifest {manifest}: {e.strerror or e}") from None
    except ValueError as e:
        raise Fail(EXIT_ERROR, f"the plugin manifest {manifest} is not valid JSON: {e}") from None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise Fail(EXIT_ERROR, f"the plugin manifest {manifest} has no non-empty string `version`")
    return version
