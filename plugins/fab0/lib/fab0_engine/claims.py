"""Write claims: which paths of which repositories a unit may change. Units whose claims overlap don't run at once."""

from __future__ import annotations

from fnmatch import fnmatchcase

EVERYTHING = "**"


def _dir(glob: str) -> str | None:
    """The directory a glob covers entirely (`docs/**`, `docs/`), or None for a file or a narrower pattern."""
    if glob == EVERYTHING:
        return ""
    for suffix in ("/**", "/"):
        if glob.endswith(suffix) and not any(c in glob[: -len(suffix)] for c in "*?["):
            return glob[: -len(suffix)]
    return None


def globs_overlap(a: str, b: str) -> bool:
    da, db = _dir(a), _dir(b)
    if da is not None and db is not None:
        return da == "" or db == "" or da == db or da.startswith(db + "/") or db.startswith(da + "/")
    if da is not None:
        return da == "" or b == da or b.startswith(da + "/") or fnmatchcase(da + "/x", b)
    if db is not None:
        return db == "" or a == db or a.startswith(db + "/") or fnmatchcase(db + "/x", a)
    return a == b or fnmatchcase(a, b) or fnmatchcase(b, a)


def overlap(a: dict[str, list[str]], b: dict[str, list[str]]) -> bool:
    """Claims are {repo: [globs]}; they overlap when two globs of one repository do."""
    return any(globs_overlap(x, y) for repo in a.keys() & b.keys() for x in a[repo] for y in b[repo])
