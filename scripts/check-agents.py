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


def task_workspace(root: Path) -> Workspace:
    """A workspace whose spec is merged and whose tasks are planned, ready for T1."""
    ws = Workspace(root)
    ws.do_spec()
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
        ws = task_workspace(tmp / "implementer")
        handout = ws.next("--task", "T1", user="backend-dev")
        ws.checkout(ws.api, handout["branch"], handout["start_from"])
        message, session = run_agent("fab0:implementer", handout, ws.api, [ws.home], args)
        rec = ws.record("implement", extract_json(message), task="T1", expect=None)
        results.append(
            check("implementer", rec.get("ok") and rec.get("verified"), session, {"message": message, **rec})
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
