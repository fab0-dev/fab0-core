"""Exit check for the core agents: each one, given a real brief from the fixture workspace, returns JSON that
`fab0-resolve record` accepts (valid return; for the spec author, verified outputs too; the write scope intact).

It runs real headless Claude Code sessions, so it costs tokens and needs a logged-in `claude`. Not part of pytest.

    uv run python scripts/check-agents.py [--model sonnet] [--budget 2] [--only spec-author] [--keep]
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from conftest import Workspace  # noqa: E402

PLUGIN = ROOT / "plugins" / "fab0"
TOOLS = {
    "fab0:spec-author": "Read,Grep,Glob,Write,Edit,Skill",
    "fab0:implementer": "Read,Grep,Glob,Edit,Write,Bash,Skill",
    "fab0:reviewer": "Read,Grep,Glob,Bash",
}


BOOKING = """def extend(booking: dict, room_free: bool) -> dict:
    \"\"\"Extend a booking by one night if the room is free that night.\"\"\"
    if not room_free:
        raise ValueError("the room is taken that night")
    return {**booking, "nights": booking["nights"] + 1}
"""

BOOKING_TEST = """import pytest

from src.booking import extend


def test_extends_by_one_night_when_free():
    assert extend({"nights": 2}, room_free=True) == {"nights": 3}


def test_refuses_when_taken():
    with pytest.raises(ValueError):
        extend({"nights": 2}, room_free=False)
"""


# The baseline: one scenario, concrete enough to build without asking; T1 and T2 both trace to it.
SPEC_BASE = """# Booking extension

Guests extend a booking by one night.

## Scenarios

- S1: `extend(booking, room_free)` in src/booking.py adds one night to a booking when the room is free, and raises
  ValueError when it is not. A booking is a dict `{"nights": int}`, and extend returns a new dict. It is a pure
  function; it has no storage and no HTTP route.
"""

# S6 and S7: S2 is another task's, S3 is nobody's.
SPEC_GAPS = """# Booking extension

Guests extend a booking by one night, and see it.

## Scenarios

- S1: `extend(booking, room_free)` in src/booking.py adds one night to a booking when the room is free, and raises
  ValueError when it is not. A booking is a dict `{"nights": int}`, and extend returns a new dict. It is a pure
  function; it has no storage and no HTTP route.
- S2: the booking screen shows the new number of nights.
- S3: a guest is emailed a receipt when a booking is extended.
"""

TASKS_GAPS = """- id: T1
  title: Booking API extension
  repo: backend
  files: [src/booking.py, tests/test_booking.py]
  traces_to: [S1]
- id: T2
  title: Booking screen
  repo: home
  files: [web/booking.html]
  depends_on: [T1]
  traces_to: [S2]
"""

# S8: T1's own scenario needs a change in src/rooms.py, which its task doesn't list.
SPEC_OUTSIDE = """# Booking extension

Guests extend a booking by one night.

## Scenarios

- S1: `extend(booking, room_free)` in src/booking.py adds one night to a booking when the room is free, and raises
  ValueError when it is not. The existing `is_free(room, night)` in src/rooms.py must also be changed to treat a
  night as taken when the room is closed for cleaning, so that `room_free` is computed correctly. A booking is a
  dict `{"nights": int}`, and a room is a dict with `booked` and `cleaning` lists of night strings.
"""

TASKS_OUTSIDE = """- id: T1
  title: Booking API extension
  repo: backend
  files: [src/booking.py, tests/test_booking.py]
  traces_to: [S1]
"""


def task_workspace(root: Path, spec: str | None = None, tasks: str | None = None) -> Workspace:
    """A workspace whose spec is merged and whose tasks are planned, ready for T1."""
    ws = Workspace(root)
    kwargs = {k: v for k, v in (("spec", spec), ("tasks", tasks)) if v is not None}
    ws.do_spec(**kwargs)
    ws.do_plan()
    return ws


def run_agent(agent: str, handout: dict, cwd: Path, add_dirs: list[Path], args) -> tuple[str, dict]:
    """Run one agent headlessly on a hand-out's brief; return its final message and the session's JSON."""
    cmd = [
        "claude",
        "-p",
        "--plugin-dir",
        str(PLUGIN),
        "--agent",
        agent,
        "--model",
        args.model or handout["model"],
        "--output-format",
        "json",
        "--max-budget-usd",
        str(args.budget),
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        TOOLS[agent],
    ]
    for folder in add_dirs:
        cmd += ["--add-dir", str(folder)]
    run = subprocess.run(cmd, input=handout["brief"], cwd=cwd, capture_output=True, text=True)
    try:
        session = json.loads(run.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"{agent}: claude failed (exit {run.returncode}):\n{run.stdout}\n{run.stderr}") from None
    return session.get("result") or "", session


def extract_json(message: str) -> str:
    """The JSON object in an agent's final message, unwrapped from a code fence if there is one."""
    fenced = re.search(r"```(?:json)?\s*\n(.*?)\n```", message, re.S)
    text = fenced.group(1) if fenced else message
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def check(name: str, ok: bool, session: dict, detail) -> bool:
    cost = session.get("total_cost_usd")
    print(f"{'PASS' if ok else 'FAIL'}  {name:<16} cost ${cost or 0:.2f}  turns {session.get('num_turns')}")
    if not ok:
        print(json.dumps(detail, indent=2) if not isinstance(detail, str) else detail)
    return ok


ROOMS = """def is_free(room: dict, night: str) -> bool:
    return night not in room.get("booked", [])
"""


def run_implementer(root: Path, args, spec: str | None = None, tasks: str | None = None, seed: dict | None = None):
    """Hand T1 of a workspace to the implementer; return the workspace, session, message, JSON and record result."""
    ws = task_workspace(root, spec, tasks)
    handout = ws.next("--task", "T1", user="backend-dev")
    ws.checkout(ws.api, handout["branch"], handout["start_from"])
    for rel, text in (seed or {}).items():
        ws.write(ws.api, rel, text)
    if seed:
        ws.commit(ws.api, "seed")
    message, session = run_agent("fab0:implementer", handout, ws.api, [ws.home], args)
    returned = extract_json(message)
    rec = ws.record("implement", returned, task="T1", expect=None)
    return ws, session, message, returned, rec


def questions_of(returned: str) -> list[str]:
    try:
        return json.loads(returned).get("questions") or []
    except (json.JSONDecodeError, AttributeError):
        return []


def changed_paths(repo: Path) -> list[str]:
    """Paths the working tree changed or added since the last commit, untracked files listed one by one."""
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True
    ).stdout
    return [line[3:] for line in out.splitlines()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", help="run every agent on this model instead of the one the brief resolves")
    parser.add_argument("--budget", type=float, default=2.0, help="USD budget per agent session (default 2)")
    parser.add_argument("--only", choices=["spec-author", "implementer", "reviewer"])
    parser.add_argument("--keep", action="store_true", help="keep the temporary workspaces")
    args = parser.parse_args()
    results = []
    tmp = Path(tempfile.mkdtemp(prefix="fab0-agents-"))

    if args.only in (None, "spec-author"):
        ws = Workspace(tmp / "spec")
        handout = ws.next()
        ws.checkout(ws.home, handout["branch"], handout["start_from"])
        message, session = run_agent("fab0:spec-author", handout, ws.home, [ws.api], args)
        rec = ws.record("spec", extract_json(message), expect=None)
        results.append(
            check("spec-author", rec.get("ok") and rec.get("verified"), session, {"message": message, **rec})
        )

    if args.only in (None, "implementer"):
        # The fixture's T1 is fully deliverable and its only scenario is traced: any question would be a false gap.
        ws, session, message, returned, rec = run_implementer(tmp / "implementer", args, SPEC_BASE)
        questions = questions_of(returned)
        ok = bool(rec.get("ok") and rec.get("verified")) and not questions
        results.append(check("implementer", ok, session, {"message": message, "questions": questions, **rec}))

        # S6 and S7: no question about S2 (T2 delivers it), a question about S3 (no task delivers it).
        ws, session, message, returned, rec = run_implementer(
            tmp / "implementer-gaps", args, SPEC_GAPS, TASKS_GAPS
        )
        questions = questions_of(returned)
        # A question may mention S2 only to say another task delivers it, and then it must be about S3.
        s2_delivered = r"\bS2\b[^.]*\b(T2|other task|another task)"
        about_s2 = [
            q for q in questions
            if re.search(r"\bS2\b", q) and not (re.search(r"\bS3\b", q) and re.search(s2_delivered, q, re.I))
        ]
        no_task = r"\bno (other )?task|not traced|any task|nobody|no one"
        about_s3 = [q for q in questions if re.search(r"\bS3\b", q) and re.search(no_task, q, re.I)]
        ok = bool(rec.get("ok") and rec.get("verified")) and not about_s2 and bool(about_s3)
        results.append(
            check("impl-gaps", ok, session, {"message": message, "questions": questions, "S2": about_s2, **rec})
        )

        # S8: the file outside the task stays unchanged, and a question names it.
        ws, session, message, returned, rec = run_implementer(
            tmp / "implementer-outside", args, SPEC_OUTSIDE, TASKS_OUTSIDE, {"src/rooms.py": ROOMS}
        )
        questions = questions_of(returned)
        changed = changed_paths(ws.api)
        own = ("src/booking.py", "tests/test_booking.py")
        outside = [
            p for p in changed if p not in own and not p.startswith("work/") and "__pycache__" not in p
        ]
        why = r"because|needs?\b|so that|must|require|in order to|to treat"
        named = [q for q in questions if "rooms.py" in q and re.search(why, q, re.I)]
        ok = bool(rec.get("ok") and rec.get("verified")) and not outside and bool(named)
        results.append(
            check("impl-outside", ok, session, {"message": message, "questions": questions, "outside": outside, **rec})
        )

    if args.only in (None, "reviewer"):
        # The reviewer judges a known implementation, so its check doesn't depend on the implementer's run.
        ws = task_workspace(tmp / "reviewer")
        handout = ws.next("--task", "T1", user="backend-dev")
        ws.checkout(ws.api, handout["branch"], handout["start_from"])
        ws.write(ws.api, "src/booking.py", BOOKING)
        ws.write(ws.api, "tests/test_booking.py", BOOKING_TEST)
        ws.record("implement", {"summary": "Added extend() with a room check, and its tests."}, task="T1")
        ws.commit(ws.api, "T1")
        gate = ws.next("--task", "T1", user="backend-dev")
        if gate["kind"] != "gate":
            results.append(check("reviewer", False, {}, {"expected a gate, got": gate}))
        else:
            message, session = run_agent("fab0:reviewer", gate, ws.api, [ws.home], args)
            rec = ws.record("implement", extract_json(message), task="T1", gate=True, expect=None)
            results.append(check("reviewer", rec.get("ok") is True, session, {"message": message, **rec}))

    print(f"workspaces: {tmp}" if args.keep else "", end="")
    if not args.keep:
        subprocess.run(["rm", "-rf", str(tmp)])
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
