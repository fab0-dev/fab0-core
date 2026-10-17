"""A run's event log: what the engine handed out, recorded, did and found, and when.

The log is `<home git common dir>/fab0/events/<run>.jsonl`, beside the pending hand-outs: one JSON object per
line, append-only, shared by every checkout of the home repo and every process on the machine, and outside every
branch, so writing it moves nothing in git. An event holds only what the engine's lines and states already show.

An event's `unit` is a unit id, except for the run waiting for the merge of its own pull requests: that event
names the run branch (as the inbox does), and a reader must not take it for a unit.

`fab0-resolve log` prints it (`Tail`). Only `next`, `record` and `apply` write.
A failed write never changes a command's result: it becomes a warning.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema

from .ledger import now
from .schedule import Run
from .schemas import core_schema, schema_errors

STATES = ("waiting", "blocked")  # the kinds of a state event
HUMAN = ("questions", "escalated")  # reasons a unit waits on a person, besides a human gate


def events_path(run: Run) -> Path:
    return run.home.common_dir() / "fab0" / "events" / f"{run.run}.jsonl"


def parse_lines(data: bytes, first: int = 1) -> list[tuple[int, dict | None]]:
    """Each complete line of `data` as (its number, its event), numbered from `first`; the event is None when
    the line isn't a valid event. `data` holds whole lines only: each ends with a newline."""
    schema = core_schema("event")
    validator = jsonschema.Draft202012Validator(schema)  # once per read, not once per line
    found: list[tuple[int, dict | None]] = []
    for number, raw in enumerate(data.split(b"\n")[:-1], first):
        try:
            event = json.loads(raw)
        except ValueError:
            event = None
        if not isinstance(event, dict) or schema_errors(event, schema, validator):
            event = None
        found.append((number, event))
    return found


def read_events(path: Path) -> list[dict]:
    """The complete, valid events of a log, oldest first. A last line without its end is still being written,
    and a line that isn't an event is skipped."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    return [event for _, event in parse_lines(data) if event is not None]


def event_text(number: int, event: dict | None) -> str:
    """An event as `log` prints it: its time and its line, on one line. An event that can't be read is said so."""
    if event is None:
        return f"event {number} can't be read"
    return f"{event['time']}  {' '.join(event['line'].splitlines())}"


class Tail:
    """What `log` prints, a poll at a time: the complete lines appended since the last poll, as text. A last
    line without its end is still being written; it is left until its end is there, and then taken once."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0  # bytes already taken
        self.number = 0  # lines already taken

    def poll(self) -> list[str]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        if not end:
            return []
        lines = [event_text(n, event) for n, event in parse_lines(data[:end], self.number + 1)]
        self.offset += end
        self.number += len(lines)
        return lines


def append_event(path: Path, event: dict) -> None:
    """Add one event as one complete line in one write, so commands running at once never interleave."""
    errors = schema_errors(event, core_schema("event"))
    if errors:
        raise ValueError(f"invalid event: {'; '.join(errors)}")
    data = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if os.write(fd, data) != len(data):
            raise OSError("short write")
    finally:
        os.close(fd)


def reason_text(reason: dict | None) -> str:
    """A reason as the board and the log show it: `gate human`, `tracker acme/shop#9`."""
    parts = []
    for key, value in (reason or {}).items():
        if isinstance(value, list):
            value = ", ".join(map(str, value))
        elif isinstance(value, dict):
            value = ", ".join(f"{k} {v}" for k, v in value.items())
        parts.append(f"{key} {value}")
    return "; ".join(parts)


def state_line(state: str, unit: str, reason: dict | None) -> str:
    """One line for a state: `waiting: spec (gate human)`."""
    text = reason_text(reason)
    return f"{state}: {unit}" + (f" ({text})" if text else "")


def apply_text(entry: dict) -> tuple[str, str | None]:
    """An entry of `apply`'s `applied` as the line of its event, and its unit, if it has one."""
    if "store" in entry:
        return f"apply: store: {entry['store']}", None
    if "item" in entry:
        return f"apply: {entry['item']}: signal {entry['signal']}", None
    unit = entry["unit"]
    if "action" in entry:
        said = f"{entry['action']}: {entry['did']}"
    else:
        said = next(entry[k] for k in ("merge", "pull", "sync") if k in entry)
    return f"apply: {unit}: {said}", unit


class EventLog:
    """The events of one command of one run. Every method that writes keeps failing quietly: `warnings` says
    why, and the command's result stays what it would be without a log."""

    def __init__(self, run: Run):
        self.run = run
        self.warnings: list[str] = []
        self.failed = False
        self._path: Path | None = None

    def path(self) -> Path:
        if self._path is None:
            self._path = events_path(self.run)
        return self._path

    def fail(self, exc: Exception) -> None:
        self.failed = True
        self.warnings.append(f"the run's event couldn't be logged: {exc}")

    def add(self, kind: str, line: str, **fields) -> bool:
        if self.failed:
            return False
        event = {"time": now(), "kind": kind, "line": line, **{k: v for k, v in fields.items() if v is not None}}
        try:
            append_event(self.path(), event)
        except (OSError, ValueError) as exc:
            self.fail(exc)
            return False
        return True

    def existing(self) -> list[dict]:
        if self.failed:
            return []
        try:
            return read_events(self.path())
        except OSError as exc:
            self.fail(exc)
            return []

    def handouts(self, outs: list[dict]) -> None:
        """One event per hand-out of a unit or a gate; an action's hand-out has no line and adds none."""
        for out in outs:
            if out.get("line"):
                self.add("handout", out["line"], unit=out["unit"], attempt=out.get("attempt"))

    def recorded(self, result: dict) -> None:
        """The result of `record`, or of `record --gate`: its end line as the command returned it."""
        self.add(
            "gate" if result.get("gate") else "record", result["line"], unit=result["unit"], attempt=result["attempt"]
        )

    def applied(self, applied: list[dict]) -> None:
        """One event per entry of `apply`'s `applied`, in order, except a unit's entry that repeats the last
        `apply` event of that unit: `apply` reports a merge that waits for checks every time. An item's or a
        store push is a new action each time, and is always logged."""
        if not applied:
            return
        last: dict[str, str] = {}
        for event in self.existing():
            if event["kind"] == "apply" and event.get("unit"):
                last[event["unit"]] = event["line"]
        for entry in applied:
            line, unit = apply_text(entry)
            if unit and last.get(unit) == line:
                continue
            if self.add("apply", line, unit=unit) and unit:
                last[unit] = line

    def states(self, result: dict) -> None:
        """The units that wait on a person or are blocked by a signal, and the run waiting for the merge of its
        own pull requests: one event each time its state, reason and attempt differ from its last state event."""
        from .inbox import SIGNALS  # not at the top: the inbox reads events too

        due: list[tuple[str, str, dict, int | None]] = []
        for unit in self.run.units:
            reason = unit.reason or {}
            # a step has an attempt, and so has a block with an auto gate (one per decision of its gate)
            attempt = unit.attempt if unit.step or unit.auto is not None else None
            if unit.state == "waiting" and (reason.get("gate") == "human" or set(reason) & set(HUMAN)):
                due.append(("waiting", unit.id, reason, attempt))
            elif unit.state == "blocked" and set(reason) & set(SIGNALS):
                due.append(("blocked", unit.id, reason, attempt))
        if result.get("kind") == "waiting" and self.run.run_branch:
            # the run's own pull request: its `unit` is the run branch, not a unit id
            due.append(("waiting", self.run.run_branch, result.get("reason") or {}, None))
        if not due:
            return
        last: dict[str, tuple] = {}
        for event in self.existing():
            if event["kind"] in STATES and event.get("unit"):
                last[event["unit"]] = (event["kind"], event.get("reason"), event.get("attempt"))
        for kind, unit, reason, attempt in due:
            text = reason_text(reason)
            if last.get(unit) == (kind, text or None, attempt):
                continue
            self.add(kind, state_line(kind, unit, reason), unit=unit, attempt=attempt, reason=text or None)
