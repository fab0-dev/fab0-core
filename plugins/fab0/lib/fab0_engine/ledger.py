"""The ledger: one fact per attempt of a unit, one per gate decision, written by `record` into the run store
(D74), and one per claim, written by `next` before it hands an attempt out (D81):
`ledger/<unit>-<attempt>[-gate|-claim].yaml` in the run's folder."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import yaml

from .errors import EXIT_ERROR, Fail
from .schemas import core_schema, schema_errors

ATTEMPT = re.compile(r"^(?P<unit>.+)-(?P<attempt>[1-9][0-9]*)$")


def entry_name(unit: str, attempt: int, gate: bool = False) -> str:
    """A fact's name in the store: `<unit>-<attempt>`, or `<unit>-<attempt>-gate` for a gate decision."""
    return f"{unit}-{attempt}{'-gate' if gate else ''}"


def claim_name(unit: str, attempt: int) -> str:
    return f"{unit}-{attempt}-claim"


def attempts(names, unit: str) -> list[int]:
    """The attempts of `unit` among fact names, oldest first."""
    pattern = re.compile(rf"^{re.escape(unit)}-([1-9][0-9]*)$")
    return sorted(int(m.group(1)) for n in names if (m := pattern.match(n)))


def gate_attempts(names, unit: str) -> list[int]:
    """The attempts of `unit` that an auto gate has decided on."""
    pattern = re.compile(rf"^{re.escape(unit)}-([1-9][0-9]*)-gate$")
    return sorted(int(m.group(1)) for n in names if (m := pattern.match(n)))


def parse(raw: bytes | str, where: str) -> dict:
    try:
        entry = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise Fail(EXIT_ERROR, f"invalid ledger entry {where}: {exc}") from None
    return check(entry, where)


def check(entry, where: str) -> dict:
    """A ledger entry checked against its schema: a claim's (D81), by its name, or an attempt's or gate's."""
    errors = schema_errors(entry, core_schema("claim" if where.endswith("-claim") else "ledger-entry"))
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
