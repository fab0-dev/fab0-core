"""The ledger: one YAML file per attempt of a unit, written by `record`, carried to the base branch by merges."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import yaml

from .errors import EXIT_ERROR, Fail
from .schemas import core_schema, schema_errors

DEFAULT_PATH = "work/{run}/ledger"


def entry_name(unit: str, attempt: int, gate: bool = False) -> str:
    return f"{unit}-{attempt}{'-gate' if gate else ''}.yaml"


def attempts(names: set[str], unit: str) -> list[int]:
    """The attempts of `unit` among ledger file names, oldest first."""
    pattern = re.compile(rf"^{re.escape(unit)}-([1-9][0-9]*)\.yaml$")
    return sorted(int(m.group(1)) for n in names if (m := pattern.match(n)))


def gate_attempts(names: set[str], unit: str) -> list[int]:
    """The attempts of `unit` that an auto gate has decided on."""
    pattern = re.compile(rf"^{re.escape(unit)}-([1-9][0-9]*)-gate\.yaml$")
    return sorted(int(m.group(1)) for n in names if (m := pattern.match(n)))


def parse(raw: bytes | str, where: str) -> dict:
    try:
        entry = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise Fail(EXIT_ERROR, f"invalid ledger entry {where}: {exc}") from None
    errors = schema_errors(entry, core_schema("ledger-entry"))
    if errors:
        raise Fail(EXIT_ERROR, f"invalid ledger entry {where}", errors=errors)
    return entry


def dump(entry: dict) -> str:
    return yaml.safe_dump(entry, sort_keys=False, allow_unicode=True)


TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now() -> str:
    """The clock, as the ledger keeps it: UTC, whole seconds, RFC 3339 (`2026-10-15T10:04:12Z`)."""
    return datetime.now(UTC).strftime(TIME_FORMAT)


def seconds_between(started: str, ended: str) -> int:
    """Whole seconds from `started` to `ended`; never negative, so a clock that moved back gives 0."""
    delta = datetime.strptime(ended, TIME_FORMAT) - datetime.strptime(started, TIME_FORMAT)
    return max(0, int(delta.total_seconds()))


def took(seconds: int) -> str:
    """How long, for a person: `42s`, `4m 12s`, `1h 3m`. The one place durations are formatted."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def timing(started: str | None, ended: str) -> dict:
    """The times of a `record` result. Without a start (a hand-out saved before times existed) only `ended`."""
    if not started:
        return {"ended": ended}
    seconds = seconds_between(started, ended)
    return {"started": started, "ended": ended, "duration": seconds, "took": took(seconds)}
