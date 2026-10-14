"""Hand-outs (`next`) and results (`record`): what a session needs to execute a unit, and the ledger entry after.

A hand-out is saved under the home repo's git directory (`fab0/pending/<run>/<unit>.json`) until `record`
consumes it, so a unit is recorded only as it was handed out.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

from .artifacts import content_hash, input_hashes, read_disk, verify, view
from .claims import covers
from .errors import EXIT_REFUSED, EXIT_USAGE, Fail
from .git import scope_diff
from .ledger import dump as dump_entry
from .ledger import entry_name, parse
from .schedule import Run, Unit
from .schemas import core_schema, load_schema, resolve_schema_ref, schema_errors
from .templates import Unresolved, render
from .version import read_version
from .wiring import HARNESS


def return_schema(run: Run, step: dict) -> dict:
    schema = copy.deepcopy(core_schema("return"))
    extensions = schema.pop("$defs")["extensions"]
    returns = step.get("return", {})
    if "questions" in returns:
        schema["properties"]["questions"] = extensions["questions"]
        if returns["questions"] == "required":
            schema["required"].append("questions")
    if "schema" in returns:
        schema["properties"]["data"] = load_schema(resolve_schema_ref(returns["schema"], run.w.root))
        schema["required"].append("data")
    return schema


def worktree(run: Run, repo_name: str, branch: str, unit: str) -> str:
    """Where the unit works: the checkout that has its branch, or else a worktree the session adds at this path."""
    repo = run.repos[repo_name]
    existing = repo.worktrees().get(branch)
    return str(existing) if existing else str(repo.common_dir() / "fab0" / "worktrees" / run.run / unit)


def branches(run: Run, unit: Unit) -> list[dict]:
    """The branches the session needs, in each repository the unit works in, oldest first.

    The last one in each repository is the unit's own, with the `worktree` to check it out in. An existing
    branch marked `sync` gets its `from` merged in before work starts: a rework after its parent moved. Never
    a rebase, since nothing is force-pushed.
    """
    if run.ledger_branch:
        repo, b = run.repos["home"], run.ledger_branch
        tree = worktree(run, "home", b, unit.id)
        return [{"repo": "home", "branch": b, "from": repo.base, "exists": bool(repo.fresh(b)), "worktree": tree}]
    out = []
    for name in unit.write or [unit.ledger_repo]:
        repo = run.repos[name]
        chain, branch = [], unit.branch
        parent = unit.ctx.parent
        while True:
            exists = bool(repo.fresh(branch))
            chain.append({"repo": name, "branch": branch, "from": run.parent_in(repo, parent), "exists": exists})
            if parent is None or repo.fresh(parent):
                break
            branch, parent = parent, run.branch_parents.get(parent)
        chain[0]["worktree"] = worktree(run, name, unit.branch, unit.id)
        head, base = repo.fresh(unit.branch), repo.fresh(chain[0]["from"])
        if head and base and not repo.is_ancestor(base, head):
            chain[0]["sync"] = True  # merge `from` in first; `conflicts` lists what the merge leaves to resolve
            conflicts = repo.merge_conflicts(head, base)
            if conflicts:
                chain[0]["conflicts"] = conflicts
        out.extend(reversed(chain))
    return out


def rework_lines(unit: Unit) -> list[str]:
    """Why this attempt exists, when it isn't the first."""
    why = unit.rework or {}
    if not why:
        return []
    lines = [
        "## Rework",
        "",
        f"Attempt {unit.attempt - 1} is on your branch; build on it, and address all of this:",
        "",
    ]
    if "stale" in why:
        lines.append("- These inputs changed since then, so read them again: " + ", ".join(why["stale"]) + ".")
    if "missing" in why:
        lines.append("- These outputs are gone and must be written again: " + ", ".join(why["missing"]) + ".")
    for name, errors in why.get("verify", {}).items():
        lines.append(f"- {name} failed its checks:")
        lines += [f"  - {e}" for e in errors]
    for repo, files in why.get("conflict", {}).items():
        lines.append(f"- Your branch conflicts with the branch it merges into, in {repo}: " + ", ".join(files) + ".")
        lines.append(
            "  The session merged that branch into yours, so these files hold conflict markers. Resolve each "
            "one so both sides' intent survives, and remove the markers."
        )
    for pr, names in why.get("checks", {}).items():
        lines.append(f"- The required checks of {pr} fail: " + (", ".join(names) or "see the pull request") + ".")
    rejected = why.get("rejected")
    if rejected is not None:
        lines.append("- The gate rejected it:")
        lines += [f"  - {r}" for r in rejected.get("reasons", [])]
        for f in rejected.get("findings", []):
            where = f" ({f['location']})" if f.get("location") else ""
            check = f"{f['check']}: " if f.get("check") else ""
            lines.append(f"  - {check}{f['message']}{where}")
    if "previous" in why:
        lines += ["", "What the previous attempt said it did:", "", why["previous"]]
    return lines + [""]


def brief(run: Run, unit: Unit, schema: dict, trees: dict[str, str]) -> str:
    step = unit.step
    title = f"# {step['step']}" + (f" for {unit.element_id}" if unit.element_id else "") + f": run {run.run}"
    lines = [title, "", f"Pipeline {run.name}, attempt {unit.attempt}.", f"Handed out by fab0 {read_version()}.", ""]
    lines += rework_lines(unit)
    if "instructions" in step:
        lines += ["## Instructions", "", step["instructions"], ""]
    if unit.ctx.element:
        lines += ["## Unit", "", "````json", json.dumps(unit.ctx.element["raw"], indent=2), "````", ""]
    inputs = [n for n in step.get("inputs", []) if n != "unit" and not n.startswith("unit.")]
    if inputs:
        lines += ["## Inputs", ""]
        for name in inputs:
            lines += [f"### {name}", "", view(run, name, unit.ctx, unit).text, ""]
    if step.get("outputs"):
        lines += ["## Outputs", ""]
        for name in step["outputs"]:
            art = run.w.artifacts[name]
            if art["kind"] == "file":
                repo, path = run.artifact_location(name, unit.ctx)
                lines.append(f"- {name}: write `{path}` in `{trees.get(repo.name, repo.path)}`")
            else:
                lines.append(f"- {name}")
        lines.append("")
    lines += ["## Where you work", ""]
    if unit.write:
        for repo in unit.write:
            globs = ", ".join(f"`{g}`" for g in unit.claims.get(repo, []))
            lines.append(f"- You may change `{trees.get(repo, run.repos[repo].path)}`, only {globs}.")
    else:
        lines.append("- You change no files.")
    reads = step.get("repos", {}).get("read", [])
    if reads:
        names = list(run.repos) if "all" in reads else reads
        lines.append("- You may read " + ", ".join(f"`{run.repos[n].path}`" for n in names if n in run.repos) + ".")
    lines += ["- Don't commit, push, create or switch branches, or run gh; that happens after you reply.", ""]
    if step.get("skills"):
        lines += ["## Skills", "", "Load these with the Skill tool first: " + ", ".join(step["skills"]) + ".", ""]
    lines += [
        "## Reply",
        "",
        "Reply with one JSON object and nothing else, valid against this schema:",
        "",
        "````json",
        json.dumps(schema, indent=2),
        "````",
    ]
    return "\n".join(lines) + "\n"


def action_handout(run: Run, unit: Unit) -> dict:
    """An action whose check fails: `apply` performs it, and its check is its record (D37)."""
    out = {
        "unit": unit.id,
        "executor": "action",
        "action": unit.step["action"],
        "missing": (unit.reason or {}).get("missing"),
        "fab0_version": read_version(),
    }
    if unit.lifecycle:
        out["lifecycle"] = unit.lifecycle
    try:
        if unit.step["action"] == "tracker.child.sync":
            from .actions import child_titles

            args = unit.step.get("with", {})
            out["with"] = {k: v for k, v in args.items() if k != "title"}
            out["with"]["items"] = [
                {"id": e["id"], "title": title, "after": e["after"]}
                for e, title in child_titles(run, args, unit.ctx, unit.elements or [])
            ]
        else:
            out["with"] = render(unit.step.get("with", {}), run.lookup(unit.ctx))
    except Unresolved as exc:
        raise Fail(EXIT_REFUSED, f"{unit.id}: {{{exc}}} has no value yet") from None
    return out


def handout(run: Run, unit: Unit) -> dict:
    step = unit.step
    executor = next(k for k in ("agent", "action", "command", "tool") if k in step)
    out = {
        "unit": unit.id,
        "step": step["step"],
        "attempt": unit.attempt,
        "executor": executor,
        "repo": unit.ledger_repo,
        "branch": run.ledger_branch or unit.branch,
        "branches": branches(run, unit),
        "write": unit.write,
        "claims": unit.claims,
        "gate": unit.gate,
        "fab0_version": read_version(),
    }
    if unit.element_id:
        out["element_id"] = unit.element_id
        out["element"] = unit.ctx.element["raw"]
    lookup = run.lookup(unit.ctx)
    try:
        if executor == "agent":
            schema = return_schema(run, step)
            out["agent"] = step["agent"]
            out["model"] = run.w.project["models"][HARNESS][step["model"]]
            out["skills"] = step.get("skills", [])
            out["return_schema"] = schema
            trees = {b["repo"]: b["worktree"] for b in out["branches"] if "worktree" in b}
            out["brief"] = brief(run, unit, schema, trees)
        elif executor == "command":
            out["command"] = render(step["command"], lookup)
        else:
            out[executor] = step[executor]
            out["with"] = render(step.get("with", {}), lookup)
            if executor == "tool":
                out["return_schema"] = return_schema(run, step)
    except Unresolved as exc:
        raise Fail(EXIT_REFUSED, f"{unit.id}: {{{exc}}} has no value yet") from None
    return out


def claim(run: Run, unit: Unit) -> bool:
    """Claim a unit that starts now by creating its branch, on the remote first (D46). False: someone else has."""
    from .git import Writer

    if run.ledger_branch or unit.state != "ready":
        return True
    for name in dict.fromkeys(unit.write or [unit.ledger_repo]):
        repo = run.repos[name]
        parent = repo.fresh(run.parent_in(repo, unit.ctx.parent))
        if repo.fresh(unit.branch) or parent is None:
            continue  # a rework's branch is the unit's already; a parent that doesn't exist yet is the session's
        if not Writer(repo).claim(unit.branch, repo.sha(parent), run.actor or "fab0"):
            return False
    return True


def workspace_root(run: Run) -> Path:
    import os

    return Path(os.path.commonpath([str(r.path.parent) for r in run.repos.values()]))


def outside_state(run: Run) -> dict[str, int]:
    """What sits beside the repositories in the workspace, and when each last changed. An agent that writes
    outside every repository usually writes there (from #20); writes anywhere else can't be seen."""
    repos = {r.path for r in run.repos.values()}
    root = workspace_root(run)
    state = {}
    for entry in root.iterdir() if root.is_dir() else []:
        if entry.resolve() in repos:
            continue
        state[entry.name] = entry.stat().st_mtime_ns
    return state


def save_pending(run: Run, unit: Unit, out: dict) -> None:
    """Keep the hand-out until `record`: the inputs' hashes, the commit each repository starts from, and the
    state of every repository the unit may not write, so changes there are seen."""
    folder = run.pending_dir()
    folder.mkdir(parents=True, exist_ok=True)
    # a parent that doesn't exist yet is cut from the oldest branch of the chain that does
    starts = {}
    for name in {b["repo"] for b in out["branches"]}:
        chain = [b for b in out["branches"] if b["repo"] == name]
        repo = run.repos[name]
        ref = repo.fresh(chain[-1]["from"]) or repo.fresh(chain[0]["from"])
        if ref:
            starts[name] = {"branch": chain[-1]["from"], "sha": repo.sha(ref)}
    others = [n for n in run.repos if n not in unit.write]
    pending = {
        **out,
        "pipeline": run.name,
        "actor": run.actor,
        "starts": starts,
        "inputs": input_hashes(run, unit),
        "scope": {n: run.repos[n].scope_state() for n in others},
        "outside": outside_state(run),
        "ledger_dir": run.ledger_dir,
        "outputs": unit.step.get("outputs", []),
        "rework": sorted(k for k in (unit.rework or {}) if k != "previous"),
    }
    pending.pop("brief", None)
    (folder / f"{unit.id}.json").write_text(json.dumps(pending, indent=2))


def check_writes(run: Run, unit: Unit, pending: dict, trees: dict) -> dict[str, list[str]]:
    """The unit changed only what it claimed, and nothing in a repository it may not write.

    Changes outside the claims are findings for the next attempt; changes in another repository refuse the
    record, because they can't be told apart from a person's own work there.
    """
    changed = {
        n: diff for n in pending["scope"] if (diff := scope_diff(pending["scope"][n], run.repos[n].scope_state()))
    }
    if changed:
        raise Fail(EXIT_REFUSED, f"{unit.id} changed repositories it may not write", changed=changed)
    findings = {}
    for name, tree in trees.items():
        start = pending["starts"].get(name)
        if not start:
            continue
        # changes since the branch last took its parent in: a synced branch's merge brings others' work along
        repo = run.repos[name]
        parent = repo.fresh(start["branch"])
        since = start["sha"]
        if parent:
            found = subprocess.run(
                ["git", "-C", str(tree), "merge-base", "HEAD", parent], capture_output=True, text=True
            )
            since = found.stdout.strip() or since
        files = repo.changed_since(tree, since)
        globs = pending["claims"].get(name, [])
        outside = [f for f in files if not f.startswith(run.ledger_dir + "/") and not any(covers(g, f) for g in globs)]
        if outside:
            findings[f"claims:{name}"] = [f"{f} is outside the claimed paths ({', '.join(globs)})" for f in outside]
    return findings


def record(run: Run, unit_id: str, returned: dict | None, exit_code: int | None) -> dict:
    found = next((u for u in run.units if u.id == unit_id), None)
    if found is not None and found.step and "action" in found.step:
        raise Fail(EXIT_REFUSED, f"{unit_id} is an action: `apply` performs it, and its check is its record")
    path = run.pending_dir() / f"{unit_id}.json"
    if not path.is_file():
        raise Fail(EXIT_REFUSED, f"{unit_id} wasn't handed out to this checkout; run next first")
    pending = json.loads(path.read_text())
    unit = next((u for u in run.units if u.id == unit_id), None)
    if unit is None:
        raise Fail(EXIT_REFUSED, f"{unit_id} is no unit of run {run.run} now; run next again")
    executor = pending["executor"]
    entry: dict = {
        "step": pending["step"],
        "attempt": pending["attempt"],
        "executor": executor,
        "actor": pending["actor"],
        "branch": pending["branch"],
    }
    if "element_id" in pending:
        entry["unit"] = pending["element_id"]
    if "model" in pending:
        entry["model"] = pending["model"]
    start = pending["starts"].get(pending["repo"])
    if not start:
        raise Fail(
            EXIT_REFUSED, f"{unit_id}: the branch it starts from, {pending['branches'][0]['from']}, doesn't exist"
        )
    entry["base"] = start
    if pending["inputs"]:
        entry["inputs"] = pending["inputs"]
    if pending.get("rework"):
        entry["rework"] = pending["rework"]
    if pending["claims"]:
        entry["claims"] = [f"{repo}:{g}" for repo, globs in pending["claims"].items() for g in globs]
    gate = pending["gate"]
    entry["gate"] = {"type": "none" if gate == "none" else "human" if gate == "human" or "human" in gate else "auto"}
    if executor == "command":
        if exit_code is None:
            raise Fail(EXIT_USAGE, "a command step is recorded with --exit")
        entry["exit"] = exit_code
        entry["summary"] = f"exit {exit_code}"
    else:
        if returned is None:
            raise Fail(EXIT_USAGE, f"this {executor} step is recorded with --return")
        schema = pending.get("return_schema") or core_schema("return")
        schema = {k: v for k, v in schema.items() if k != "$defs"}
        errors = schema_errors(returned, schema)
        if errors:
            raise Fail(EXIT_REFUSED, f"{unit_id}: the return doesn't match its schema", errors=errors)
        entry["summary"] = returned["summary"]
        if returned.get("questions"):
            entry["questions"] = returned["questions"]
        if "data" in returned:
            entry["data"] = returned["data"]
    trees = {}
    for name in {b["repo"] for b in pending["branches"]}:
        tree = run.repos[name].worktrees().get(pending["branch"])
        if tree is None:
            raise Fail(EXIT_REFUSED, f"check out {pending['branch']} in {run.repos[name].path} before recording")
        trees[name] = Path(tree)
    outputs, checks = {}, {}
    for name in pending["outputs"]:
        if run.w.artifacts[name]["kind"] != "file":
            continue
        repo, rel = run.artifact_location(name, unit.ctx)
        tree = trees.get(repo.name)
        if tree is None:
            checks[name] = [f"{rel} is in {repo.name}, which this unit doesn't write"]
            continue
        h = content_hash(read_disk(tree, rel))
        if h:
            outputs[name] = h
        errors = verify(run, name, unit.ctx, tree)
        checks[name] = errors or "pass"
    for key, errors in check_writes(run, unit, pending, trees).items():
        checks[key] = errors
    if outputs:
        entry["outputs"] = outputs
    if checks:
        entry["verify"] = checks
    name = entry_name(unit_id, pending["attempt"])
    target = trees[pending["repo"]] / pending["ledger_dir"] / name
    if target.exists():
        raise Fail(EXIT_REFUSED, f"{target} exists; the ledger is append-only")
    parse(dump_entry(entry), name)  # the entry itself must be valid
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_entry(entry))
    path.unlink()
    failed = sorted(k for k, v in checks.items() if v != "pass")
    before, now = pending.get("outside", {}), outside_state(run)
    outside = sorted(n for n in now.keys() | before.keys() if now.get(n) != before.get(n))
    return {
        "ok": True,
        "unit": unit_id,
        "attempt": pending["attempt"],
        "entry": str(target),
        "verified": not failed,
        "failed": failed,
        "recorded": entry,
        **({"outside": [str(workspace_root(run) / n) for n in outside]} if outside else {}),
    }
