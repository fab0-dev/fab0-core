"""fab0-resolve: the command line of the engine. Every command prints one JSON object.

  validate              check the project's .fab0/ wiring (schema 2)
  status RUN            every unit of a run and its state; read-only
  next RUN              the same, and a hand-out for every unit that is ready or running
  record RUN UNIT       check a unit's result and write its ledger entry
  --version             the fab0 release of this engine

`apply` comes in step R3 and `inbox` in R4 of the refactor.

Exit codes: 0 ok, 1 error, 2 usage, 3 invalid wiring, 4 refused.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from .errors import EXIT_ERROR, EXIT_INVALID, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Fail
from .handout import handout, record, save_pending
from .schedule import Run
from .tracker import make_tracker
from .version import read_version
from .wiring import Wiring

NOT_YET = {"apply": "R3", "inbox": "R4"}
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
            return folder
    raise Fail(EXIT_USAGE, f"no .fab0/fab0.yaml in {here} or above it; pass --project")


def load_wiring(args) -> Wiring:
    wiring = Wiring(find_project(args.project), getattr(args, "profile", None))
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


def load_run(args) -> Run:
    wiring = load_wiring(args)
    if not RUN_ID.match(args.run):
        raise Fail(EXIT_USAGE, f"run id '{args.run}': letters, digits, . _ - only")
    names = wiring.project["pipelines"]
    pipeline = args.pipeline or (names[0] if len(names) == 1 else None)
    if pipeline is None:
        raise Fail(EXIT_USAGE, f"pass --pipeline: this project runs {', '.join(names)}")
    if pipeline not in names:
        raise Fail(EXIT_USAGE, f"no pipeline '{pipeline}' in fab0.yaml")
    trigger = wiring.pipelines[pipeline]["trigger"]
    if "manual" in trigger and trigger["manual"].get("id") and not re.search(trigger["manual"]["id"], args.run):
        raise Fail(EXIT_USAGE, f"run id '{args.run}' doesn't match {trigger['manual']['id']}")
    tracker = make_tracker()  # read-only: issue and pull request facts, from gh or FAB0_GH_FAKE
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
    for unit in run.units:
        if unit.step is None or unit.state not in ("ready", "running"):
            continue
        if args.unit and unit.id != args.unit:
            continue
        out = handout(run, unit)
        save_pending(run, unit, out)
        handouts.append(out)
    if args.unit and not handouts:
        found = next((u.summary() for u in run.units if u.id == args.unit), None)
        raise Fail(EXIT_REFUSED, f"{args.unit} is not ready", unit=found)
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
    return record(run, args.unit, returned, args.exit)


def cmd_not_yet(args) -> dict:
    raise Fail(
        EXIT_REFUSED,
        f"'{args.command}' is not built yet in schema 2; it returns in step {NOT_YET[args.command]} of the refactor",
    )


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
    p = run_parser("record", "check a handed-out unit's result and write its ledger entry")
    p.add_argument("unit", help="the unit, as next named it")
    p.add_argument("--return", dest="return_file", help="a file with the agent's or tool's JSON return")
    p.add_argument("--exit", type=int, help="a command step's exit code")
    p.set_defaults(handler=cmd_record)

    for name, step in NOT_YET.items():
        p = sub.add_parser(name, help=f"not built yet (refactor step {step})")
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(handler=cmd_not_yet)

    args = parser.parse_args(argv)
    if args.version:
        args.handler = cmd_version
    elif not args.command:
        parser.error("a command is required")
    try:
        result, code = args.handler(args), EXIT_OK
    except Fail as fail:
        result, code = {"ok": False, **fail.payload}, fail.code
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code
