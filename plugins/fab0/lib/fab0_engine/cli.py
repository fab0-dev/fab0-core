"""fab0-resolve: the command line of the engine.

  validate              check the project's .fab0/ wiring (schema 2)
  status RUN            every unit of a run and its state; read-only
  next RUN              the same, and a hand-out for every unit that is ready or running; with --apply, after
                        performing what is due, from the same pass over the tracker
  record RUN UNIT       check a unit's result, or with --gate its gate's, commit its work and write its ledger
                        entry into the run store; reads no tracker (at most the run's item, to pick the pipeline)
  log RUN [--follow]    print the run's events, oldest first, as text; with --follow, keep printing new ones
  --version             the fab0 release of this engine

  apply RUN             perform what is due: actions, merges, pull requests (writes git and the tracker)
  inbox                 every open run, and what it needs from whom; read-only

Every command but `log` prints one JSON object; `log` prints text, and its errors are the usual object.

Exit codes: 0 ok, 1 error, 2 usage, 3 invalid wiring, 4 refused.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path

from .apply import apply as apply_run
from .apply import settle
from .errors import EXIT_ERROR, EXIT_INVALID, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Fail
from .events import EventLog, Tail
from .gates import gate_handout, record_gate, save_gate_pending
from .handout import action_handout, claim, handout, record, save_pending
from .inbox import inbox
from .schedule import Run, Unit
from .store import open_store
from .tracker import make_tracker
from .version import check_engine, read_version
from .wiring import READ_ONLY_KINDS, Wiring

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
    store = open_store(wiring.project, wiring.root)
    why = store.unreachable()
    cfg = wiring.project["store"]
    where = cfg.get("github") or str(store.path)
    if why:
        message = (
            f"store: the runs repository {where} can't be reached ({why}). Create it, or get access to it, with the "
            "fab0:github skill"
        )
        raise Fail(EXIT_INVALID, "invalid wiring", errors=[{"file": ".fab0/fab0.yaml", "message": message}])
    return {
        "ok": True,
        "project": str(wiring.root),
        "schema": 2,
        "pipelines": wiring.project["pipelines"],
        "steps": sorted(wiring.steps),
        "artifacts": sorted(wiring.artifacts),
        "store": {"runs": where, "project": store.project, "path": str(store.path)},
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


class NoTracker:
    """A tracker that answers every read with nothing, and has no writes. `record` evaluates the run against
    it: the unit and its context come from git and its saved hand-out, and nothing it checks is on the
    tracker."""

    def user(self) -> str:
        return ""

    def open_items(self, repo, label, issue_type) -> list:
        return []

    def issue(self, repo, number):
        return None

    def sub_issues(self, repo, number) -> list:
        return []

    def blocked_by(self, repo, number) -> list:
        return []

    def fields(self, repo, number, owner, project) -> dict:
        return {}

    def comments(self, repo, number) -> list:
        return []

    def pulls(self, repo, branch, base) -> list:
        return []

    def pull(self, repo, branch, base):
        return None

    def reviews(self, repo, number) -> list:
        return []

    def diff(self, repo, number) -> str:
        return ""

    def checks(self, repo, number) -> tuple[str, list]:
        return "pass", []

    def board(self, owner, project) -> dict:
        return {"id": None, "fields": None}

    def forget(self) -> None:
        pass


class OfflineRun(Run):
    """A run evaluated without the tracker, for `record`. Its units and their contexts come from git; their
    states aren't to be trusted, and `record` asks only that the unit is there. Two things the tracker alone
    decides would make later units drop out of the run, so they are taken as they were when the unit was
    handed out:
    - a block's onStart actions (a foreach's sub-issues) are done: the unit couldn't have been handed out else;
    - an input read from the tracker (an issue, a pull request, a tool) hashes to nothing here, so it can't
      tell a recorded attempt from a stale one: it is left out of that check.

    A unit behind a `when` on the tracker is still missing: `online` makes the run that reads it."""

    online = None

    def action_state(self, unit: Unit) -> bool:
        if unit.lifecycle == "onStart":
            unit.state, unit.reason = "done", None
            return True
        return super().action_state(unit)

    def rework(self, unit: Unit, entry: dict) -> dict:
        why = super().rework(unit, entry)
        if "stale" in why:
            stale = [k for k in why["stale"] if self.w.artifacts.get(k, {}).get("kind") not in READ_ONLY_KINDS]
            if stale:
                why["stale"] = stale
            else:
                del why["stale"]
                if set(why) <= {"previous"}:
                    return {}
        return why


def pull_store(wiring) -> None:
    """One pull of the run store before a run is read, so what others recorded counts (D74). Offline, the local
    copy is read, with a warning."""
    store = open_store(wiring.project, wiring.root)
    if store is None:
        return
    try:
        lost = store.sync("pull")
    except Fail as exc:
        WARNINGS.append(f"the run store wasn't pulled, so this reads the local copy: {exc.payload['error']}")
        return
    WARNINGS.extend(f"another machine recorded {r}/{n} first; its entry stands" for r, n in lost)


def load_run(args) -> Run:
    wiring = load_wiring(args)
    if args.command != "record":  # record pushes, which pulls first
        pull_store(wiring)
    if not RUN_ID.match(args.run):
        raise Fail(EXIT_USAGE, f"run id '{args.run}': letters, digits, . _ - only")
    tracker = make_tracker()  # issue and pull request facts, from gh or FAB0_GH_FAKE
    pipeline = pick_pipeline(wiring, args.run, args.pipeline, tracker)
    if args.command == "record":
        # past the pick, `record` reads nothing: no tracker call, not even who is running
        run = OfflineRun(wiring, pipeline, args.run, "", NoTracker())
        run.online = lambda: Run(wiring, pipeline, args.run, "", tracker)
    else:
        quiet = args.command == "status"
        run = Run(wiring, pipeline, args.run, "" if quiet else actor(args, tracker), tracker)
    if not run.home.exists():
        raise Fail(EXIT_ERROR, f"the home repo {run.home.path} is not a git repository")
    return run


def cmd_status(args) -> dict:
    return load_run(args).evaluate()


def hand_out(run: Run, only: str | None, log: EventLog) -> list[dict]:
    """A hand-out for every unit that is ready or running (a gate, for a unit that waits on one), in the run's
    order; claims what it hands out, and logs each hand-out as it is made."""
    handouts = []
    for unit in run.units:
        if only and unit.id != only:
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
    return handouts


def cmd_next(args) -> dict:
    run = load_run(args)
    if args.apply:
        # `apply` for every unit, whatever --unit says: it limits only the hand-outs. Then hand out from the
        # evaluation `apply` ended with, which is one pass over the tracker.
        run, result = settle(run)
        result["follow"] = follow_command(run)
    else:
        result = run.evaluate()
    log = EventLog(run)  # a log that fails only adds a warning
    handouts = hand_out(run, args.unit, log) if not args.apply or result["kind"] == "units" else []
    if args.unit and not handouts:
        found = next((u.summary() for u in run.units if u.id == args.unit), None)
        raise Fail(
            EXIT_REFUSED,
            f"{args.unit} is not ready",
            unit=found,
            **({"applied": result["applied"]} if args.apply else {}),
        )
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
    if not args.gate and not any(u.id == args.unit for u in run.units):
        # a unit behind a condition only the tracker can decide isn't in the run without it: read the tracker
        run = run.online()
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


def follow_command(run: Run) -> str:
    """The command that follows the run from any terminal on this machine: this engine's own path, found
    relative to this module, and the home repo's main checkout."""
    engine = Path(__file__).resolve().parents[2] / "bin" / "fab0-resolve"
    return shlex.join([str(engine), "log", run.run, "--follow", "--project", str(main_checkout(run.w.root))])


def cmd_apply(args) -> dict:
    run = load_run(args)
    return {**apply_run(run, args.unit), "follow": follow_command(run)}


def cmd_inbox(args) -> dict:
    wiring = load_wiring(args)
    pull_store(wiring)
    tracker = make_tracker()
    return inbox(wiring, tracker, actor(args, tracker), lambda run: pick_pipeline(wiring, run, None, tracker))


POLL = 1.0  # seconds between two looks at the log of a followed run


def home_common_dir(root: Path) -> Path:
    run = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if run.returncode:
        raise Fail(EXIT_ERROR, f"{root} is not a git repository: {run.stderr.strip()}")
    return Path(run.stdout.strip())


def cmd_log(args) -> int:
    """Print a run's event log as text. It reads only the home checkout's git directory: no wiring, no tracker,
    no version check."""
    root = find_project(args.project)
    if not RUN_ID.match(args.run):
        raise Fail(EXIT_USAGE, f"run id '{args.run}': letters, digits, . _ - only")
    tail = Tail(home_common_dir(root) / "fab0" / "events" / f"{args.run}.jsonl")
    if args.follow:
        # an interrupt ends it, even if it was inherited ignored; set before the first line, which a person (or a
        # test) may answer with Ctrl-C at once
        signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        lines = tail.poll()
        if not lines:
            print(f"run {args.run} has no events here yet", flush=True)
        for line in lines:
            print(line, flush=True)
        while args.follow:
            time.sleep(POLL)
            for line in tail.poll():
                print(line, flush=True)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return EXIT_OK


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
    p.add_argument(
        "--apply",
        action="store_true",
        help="perform what is due first, as `apply` does, then hand out from the same read",
    )
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

    p = sub.add_parser("log", help="print a run's events, oldest first; read-only")
    p.add_argument("run", help="the run id")
    p.add_argument("--follow", action="store_true", help="keep printing new events until interrupted")
    p.add_argument("--project", help="the home repo (default: found from the current directory)")
    p.set_defaults(handler=cmd_log)

    args = parser.parse_args(argv)
    if args.version:
        args.handler = cmd_version
    elif not args.command:
        parser.error("a command is required")
    WARNINGS.clear()
    try:
        if args.command == "log" and not args.version:
            return cmd_log(args)
        result, code = args.handler(args), EXIT_OK
        if WARNINGS:
            result = {**result, "warnings": [*WARNINGS, *result.get("warnings", [])]}
    except Fail as fail:
        result, code = {"ok": False, **fail.payload}, fail.code
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code
