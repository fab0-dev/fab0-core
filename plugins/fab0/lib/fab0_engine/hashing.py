"""Content hashes, as the ledger records them: `sha256:<hex>`."""

from __future__ import annotations

import hashlib
import json


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha_json(value) -> str:
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def sha_tree(files: dict[str, bytes]) -> str:
    """A directory: the relative path and hash of every file in it."""
    return sha("".join(f"{p}\0{sha(b)}\n" for p, b in sorted(files.items())).encode())
