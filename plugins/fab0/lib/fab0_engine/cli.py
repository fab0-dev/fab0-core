"""fab0-resolve: the command line of the engine. Every command prints one JSON object.

  validate              check the project's .fab0/ wiring (schema 2)
  status RUN            every unit of a run and its state; read-only
  next RUN              the same, and a hand-out for every unit that is ready or running
  record RUN UNIT       check a unit's result, or with --gate its gate's, and write the ledger entry
  --version             the fab0 release of this engine

  apply RUN             perform what is due: actions, merges, pull requests (writes git and the tracker)
  inbox                 every open run, and what it needs from whom; read-only

Exit codes: 0 ok, 1 error, 2 usage, 3 invalid wiring, 4 refused.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from .apply import apply as apply_run
from .errors import EXIT_ERROR, EXIT_INVALID, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Fail
from .events import EventLog
from .gates import gate_handout, record_gate, save_gate_pending
from .handout import action_handout, claim, handout, record, save_pending
from .inbox import inbox
from .schedule import Run
from .tracker import make_tracker
from .version import check_engine, read_version
from .wiring import Wiring

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def find_project(arg: str | None) -> Path:
    if arg:
        root = Path(arg).resolve()
        if not (root / ".fab0" / "fab0.yaml").is_file():
            raise Fail(EXIT_USAGE, f"no .fab0/fab0.yaml in {root}")
        return root
    here = Path.cwd().resolve()
    for folder in (here, *here.parents):
        if (folder / ".fab0" / "fab0.yaml").is_file():
            return main_checkout(folder)
    raise Fail(EXIT_USAGE, f"no .fab0/fab0.yaml in {here} or above it; pass --project")


def main_checkout(folder: Path) -> Path:
    """The main checkout of the repository `folder` is in. A unit's worktree holds its branch's copy of the
    wiring, which may be older than the project's; the main checkout's governs the run (from R6)."""
    run = subprocess.run(["git", "-C", str(folder), "worktree", "list", "--porcelain"], capture_output=True, text=True)
    first = next((line for line in run.stdout.splitlines() if line.startswith("worktree ")), None)
    if run.returncode or first is None:
        return folder
    main = Path(first.removeprefix("worktree ")).resolve()
    return main if main != folder and (main / ".fab0" / "fab0.yaml").is_file() else folder


WARNINGS: list[str] = []  # warnings of the command running; main() adds them to its result


def load_wiring(args) -> Wiring:
    root = find_project(args.project)
    WARNINGS.extend(check_engine(root))  # refuses before anything else when the engine is older than the plugin
    wiring = Wiring(root, getattr(args, "profile", None))
    if wiring.errors:
        raise Fail(EXIT_INVALID, "invalid wiring", errors=wiring.errors)
    return wiring


def cmd_version(args) -> dict:
    return {"ok": True, "fab0_version": read_version()}


def cmd_validate(args) -> dict:
    wiring = load_wiring(args)
    repos = wiring.project["repos"]
    return {
        "ok": True,
        "project": str(wiring.root),
        "schema": 2,
        "pipelines": wiring.project["pipelines"],
        "steps": sorted(wiring.steps),
        "artifacts": sorted(wiring.artifacts),
        "workspace": {
            name: {"path": str((wiring.root / cfg["path"]).resolve()), "present": (wiring.root / cfg["path"]).is_dir()}
            for name, cfg in repos.items()
        },
    }


def actor(args, tracker) -> str:
    if args.as_user:
        return args.as_user
    try:
        return tracker.user()
    except (Fail, OSError):
        pass
    name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip()
    if not name:
        raise Fail(EXIT_ERROR, "can't tell who is running: pass --as")
    return name


def triggered(wiring: Wiring, name: str, run: str, item) -> bool:
    """Whether run `run` belongs to pipeline `name`, by its trigger."""
    trigger = wiring.pipelines[name]["trigger"]
    if "manual" in trigger:
        pattern = trigger["manual"].get("id")
        return not pattern or re.search(pattern, run) is not None
    issue = trigger["issue"]
    found = item()
    if found is None:
        return False
    return ("type" not in issue or found["type"] == issue["type"]) and (
        "label" not in issue or issue["label"] in found["labels"]
    )


def pick_pipeline(wiring: Wiring, run: str, asked: str | None, tracker) -> str:
    """The pipeline a run belongs to: the one asked for, or the only one whose trigger the run matches."""
    names = wiring.project["pipelines"]
    cache: dict = {}

    def item():
        if "item" not in cache:
            try:
                cache["item"] = (
                    tracker.issue(wiring.project["repos"]["home"]["github"], int(run)) if run.isdigit() else None
                )
            except Fail:
                cache["item"] = None
        return cache["item"]

    if asked:
        if asked not in names:
            raise Fail(EXIT_USAGE, f"no pipeline '{asked}' in fab0.yaml")
        if "manual" in wiring.pipelines[asked]["trigger"] and not triggered(wiring, asked, run, item):
            raise Fail(EXIT_USAGE, f"run id '{run}' doesn't match {wiring.pipelines[asked]['trigger']['manual']['id']}")
        return asked
    if len(names) == 1:
        name = names[0]
        if "manual" in wiring.pipelines[name]["trigger"] and not triggered(wiring, name, run, item):
            raise Fail(EXIT_USAGE, f"run id '{run}' doesn't match {wiring.pipelines[name]['trigger']['manual']['id']}")
        return name
    matching = [n for n in names if triggered(wiring, n, run, item)]
    if len(matching) == 1:
        return matching[0]
    which = f"it matches {', '.join(matching)}" if matching else "it matches none of their triggers"
    raise Fail(EXIT_USAGE, f"run {run}: pass --pipeline; {which}", pipelines=names)


def load_run(args) -> Run:
    wiring = load_wiring(args)
    if not RUN_ID.match(args.run):
        raise Fail(EXIT_USAGE, f"run id '{args.run}': letters, digits, . _ - only")
    tracker = make_tracker()  # read-only: issue and pull request facts, from gh or FAB0_GH_FAKE
    pipeline = pick_pipeline(wiring, args.run, args.pipeline, tracker)
    run = Run(wiring, pipeline, args.run, actor(args, tracker) if args.command != "status" else "", tracker)
    if not run.home.exists():
        raise Fail(EXIT_ERROR, f"the home repo {run.home.path} is not a git repository")
    return run


def cmd_status(args) -> dict:
    return load_run(args).evaluate()


def cmd_next(args) -> dict:
    run = load_run(args)
    result = run.evaluate()
    handouts = []
    log = EventLog(run)  # each hand-out is logged as it is made; a log that fails only adds a warning
    for unit in run.units:
        if args.unit and unit.id != args.unit:
            continue
        if unit.step is not None and "action" in unit.step:
            if unit.state == "ready":
                handouts.append(action_handout(run, unit))
        elif unit.step is not None and unit.state in ("ready", "running"):
            if not claim(run, unit):
                unit.state, unit.reason = "running", {"claimed": "elsewhere"}
                continue
            out = handout(run, unit)
            save_pending(run, unit, out)
            log.handouts([out])
            handouts.append(out)
        elif unit.state == "waiting" and unit.auto and (unit.reason or {}).get("gate") in ("gate", "running"):
            out = gate_handout(run, unit)
            save_gate_pending(run, unit, out)
            log.handouts([out])
            handouts.append(out)
    if args.unit and not handouts:
        found = next((u.summary() for u in run.units if u.id == args.unit), None)
        raise Fail(EXIT_REFUSED, f"{args.unit} is not ready", unit=found)
    result["units"] = [u.summary() for u in run.units]  # claims made just now may have changed a state
    log.states(result)
    WARNINGS.extend(log.warnings)
    return {**result, "handouts": handouts}


def cmd_record(args) -> dict:
    run = load_run(args)
    returned = None
    if args.return_file:
        try:
            returned = json.loads(Path(args.return_file).read_text())
        except (OSError, ValueError) as exc:
            raise Fail(EXIT_USAGE, f"--return {args.return_file}: {exc}") from None
    run.evaluate()
    if args.gate:
        output = Path(args.output).read_text(errors="replace") if args.output else None
        result = record_gate(run, args.unit, returned, args.exit, output)
    else:
        result = record(run, args.unit, returned, args.exit)
    log = EventLog(run)  # after the ledger entry; a refused record raised above and logs nothing
    log.recorded(result)
    WARNINGS.extend(log.warnings)
    return result


def cmd_apply(args) -> dict:
    return apply_run(load_run(args), args.unit)


def cmd_inbox(args) -> dict:
    wiring = load_wiring(args)
    tracker = make_tracker()
    return inbox(wiring, tracker, actor(args, tracker), lambda run: pick_pipeline(wiring, run, None, tracker))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fab0-resolve", description="The fab0 engine.")
    parser.add_argument("--version", action="store_true", help="print the fab0 release of this engine")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("validate", help="check the project's .fab0/ wiring")
    p.add_argument("--project", help="the home repo (default: found from the current directory)")
    p.add_argument("--profile", help="a profile from fab0.yaml to apply")
    p.set_defaults(handler=cmd_validate)

    def run_parser(name: str, help: str):
        p = sub.add_parser(name, help=help)
        p.add_argument("run", help="the run id: the issue number of the run's item, or a manual id")
        p.add_argument("--pipeline", help="the pipeline (default: the project's only one)")
        p.add_argument("--as", dest="as_user", help="the user running it (default: the tracker login)")
        p.add_argument("--project", help="the home repo (default: found from the current directory)")
        p.add_argument("--profile", help="a profile from fab0.yaml to apply")
        return p

    run_parser("status", "every unit of a run and its state; read-only").set_defaults(handler=cmd_status)
    p = run_parser("next", "the run's units, and a hand-out for each one that is ready")
    p.add_argument("--unit", help="hand out only this unit")
    p.set_defaults(handler=cmd_next)
    p = run_parser("apply", "perform the run's due actions, merges and pull requests")
    p.add_argument("--unit", help="only this unit")
    p.set_defaults(handler=cmd_apply)
    p = run_parser("record", "check a handed-out unit's result and write its ledger entry")
    p.add_argument("unit", help="the unit, as next named it")
    p.add_argument("--return", dest="return_file", help="a file with the agent's or tool's JSON return")
    p.add_argument("--exit", type=int, help="a command's exit code")
    p.add_argument("--gate", action="store_true", help="record the unit's auto gate: its verdict, or --exit")
    p.add_argument("--output", help="a command gate's output, kept as the finding when it fails")
    p.set_defaults(handler=cmd_record)

    p = sub.add_parser("inbox", help="every open run, and what it needs from whom; read-only")
    p.add_argument("--as", dest="as_user", help="the person asking (default: the tracker login)")
    p.add_argument("--project", help="the home repo (default: found from the current directory)")
    p.add_argument("--profile", help="a profile from fab0.yaml to apply")
    p.set_defaults(handler=cmd_inbox)

    args = parser.parse_args(argv)
    if args.version:
        args.handler = cmd_version
    elif not args.command:
        parser.error("a command is required")
    WARNINGS.clear()
    try:
        result, code = args.handler(args), EXIT_OK
        if WARNINGS:
            result = {**result, "warnings": [*WARNINGS, *result.get("warnings", [])]}
    except Fail as fail:
        result, code = {"ok": False, **fail.payload}, fail.code
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code
