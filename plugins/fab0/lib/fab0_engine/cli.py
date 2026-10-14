"""fab0-resolve: the command line of the engine. Every command prints one JSON object.

  validate        check the project's .fab0/ wiring (schema 2)
  --version       the fab0 release of this engine

The run commands (next, record, apply, status) come back in steps R2b to R3 of the refactor.

Exit codes: 0 ok, 1 error, 2 usage, 3 invalid wiring, 4 refused.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .errors import EXIT_INVALID, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Fail
from .version import read_version
from .wiring import Wiring

NOT_YET = {
    "next": "R2b",
    "record": "R2b",
    "status": "R2b",
    "apply": "R3",
    "inbox": "R4",
}


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
