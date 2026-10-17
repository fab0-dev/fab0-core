"""Hand-outs (`next`) and results (`record`): what a session needs to execute a unit, and the ledger entry after.

A hand-out is saved under the home repo's git directory (`fab0/pending/<run>/<unit>.json`) until `record`
consumes it, so a unit is recorded only as it was handed out. `record` commits the unit's work on its branch,
then writes its ledger entry into the run store, with the commits it made (D74).
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from .artifacts import content_hash, input_hashes, read_disk, store_name, store_view, verify, view
from .claims import covers
from .errors import EXIT_REFUSED, EXIT_USAGE, Fail
from .git import scope_diff
from .ledger import check as check_entry
from .ledger import claim_name, entry_name, now, timing
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
    out = []
    for name in unit.write or [unit.pr_repo]:
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
    where = "is on your branch" if unit.write else "is under Outputs below"
    lines = [
        "## Rework",
        "",
        f"Attempt {unit.attempt - 1} {where}; build on it, and address all of this:",
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


def brief(run: Run, unit: Unit, schema: dict, trees: dict[str, str], outbox: Path | None = None) -> str:
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
            elif art["kind"] == "store":
                lines.append(f"- {name}: write `{store_name(name, art)}` in `{outbox}`, outside every repository")
                if unit.rework and run.latest_object(name, unit.ctx):  # a store output is on no branch to build on
                    lines += ["", f"  Its latest version, {store_view(run, name, unit.ctx).text}", ""]
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


def start_line(
    label: str,
    attempt: int,
    branch: str,
    agent: str | None = None,
    model: str | None = None,
    command: str | None = None,
    tool: str | None = None,
) -> str:
    """The line the session shows when it starts a hand-out: what runs, on which agent or command, and where."""
    if agent:
        who = f"{agent} on {model}" if model else agent
    elif command is not None:
        who = f"command `{command}`"
    else:
        who = f"tool {tool}"
    return f"{label}, attempt {attempt}: {who}, branch {branch}"


def end_line(label: str, attempt: int, outcome: str, times: dict, exit_code: int | None = None, extra: str = "") -> str:
    """The line the session shows when a hand-out is recorded: its outcome and how long it took."""
    parts = [outcome]
    if exit_code is not None:
        parts.append(f"exit {exit_code}")
    if extra:
        parts.append(extra)
    parts.append(times.get("took") or "time unknown")
    return f"{label}, attempt {attempt}: " + ", ".join(parts)


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
        if unit.step["action"] in ("tracker.child.sync", "tracker.child.close"):
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
        "repo": unit.pr_repo,
        "branch": unit.branch,
        "branches": branches(run, unit),
        "write": unit.write,
        "claims": unit.claims,
        "gate": unit.gate,
        "fab0_version": read_version(),
    }
    if unit.element_id:
        out["element_id"] = unit.element_id
        out["element"] = unit.ctx.element["raw"]
    outbox = None
    if any(run.w.artifacts[n]["kind"] == "store" for n in step.get("outputs", [])):
        outbox = outbox_of(run, unit.id, unit.attempt)
        outbox.mkdir(parents=True, exist_ok=True)
        out["outbox"] = str(outbox)
    lookup = run.lookup(unit.ctx)
    try:
        if executor == "agent":
            schema = return_schema(run, step)
            out["agent"] = step["agent"]
            out["model"] = run.w.project["models"][HARNESS][step["model"]]
            out["skills"] = step.get("skills", [])
            out["return_schema"] = schema
            trees = {b["repo"]: b["worktree"] for b in out["branches"] if "worktree" in b}
            out["brief"] = brief(run, unit, schema, trees, outbox)
        elif executor == "command":
            out["command"] = render(step["command"], lookup)
        else:
            out[executor] = step[executor]
            out["with"] = render(step.get("with", {}), lookup)
            if executor == "tool":
                out["return_schema"] = return_schema(run, step)
    except Unresolved as exc:
        raise Fail(EXIT_REFUSED, f"{unit.id}: {{{exc}}} has no value yet") from None
    out["line"] = start_line(
        unit.id,
        unit.attempt,
        out["branch"],
        agent=out.get("agent"),
        model=out.get("model"),
        command=out.get("command"),
        tool=out.get("tool"),
    )
    return out


def outbox_of(run: Run, unit: str, attempt: int) -> Path:
    """Where an attempt writes its store artifacts: under the home repo's git directory, outside every checkout.
    `record` puts what it finds there into the run store (D79)."""
    return run.home.common_dir() / "fab0" / "out" / run.run / f"{unit}-{attempt}"


def claim(run: Run, unit: Unit) -> bool:
    """Take the attempt `unit` is about to be handed out at with a claim fact in the run store, pushed before the
    hand-out (D81): False when another checkout's claim stands, here or on the remote. Then the unit's branch is
    created from its parent's head, with no commit, where it doesn't exist yet."""
    name = claim_name(unit.id, unit.attempt)
    mine = run.machine(create=True)
    if run.store.append(run.run, name, {"actor": run.actor or "fab0", "machine": mine, "at": now()}) == "exists":
        if (yaml.safe_load(run.store.fact_path(run.run, name).read_text()) or {}).get("machine") != mine:
            return False
    try:
        lost = run.store.sync("push")
    except Fail:
        return False  # not pushed, so not taken: the next `next` pushes it, or finds another checkout's
    if (run.run, name) in lost:
        return False
    for repo_name in dict.fromkeys(unit.write or [unit.pr_repo]):
        repo = run.repos[repo_name]
        parent = repo.fresh(run.parent_in(repo, unit.ctx.parent))
        if parent is not None and not repo.fresh(unit.branch):
            repo.out("branch", unit.branch, repo.sha(parent))  # a parent that doesn't exist yet is the session's
            repo._fresh.clear()
    return True


def workspace_root(run: Run) -> Path:
    import os

    return Path(os.path.commonpath([str(r.path.parent) for r in run.repos.values()]))


def outside_state(run: Run) -> dict[str, int]:
    """What sits beside the repositories in the workspace, and when each last changed. An agent that writes
    outside every repository usually writes there (from #20); writes anywhere else can't be seen."""
    repos = {r.path for r in run.repos.values()} | ({run.store.path.resolve()} if run.store else set())
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
        "outputs": unit.step.get("outputs", []),
        "rework": sorted(k for k in (unit.rework or {}) if k != "previous"),
        "started": now(),  # a hand-out saved again by a later `next` starts again
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
        outside = [f for f in files if not any(covers(g, f) for g in globs)]
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
    }
    entry["branch"] = pending["branch"]
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
    trees = {}
    for name in {b["repo"] for b in pending["branches"]}:
        tree = run.repos[name].worktrees().get(pending["branch"])
        if tree is None:
            raise Fail(EXIT_REFUSED, f"check out {pending['branch']} in {run.repos[name].path} before recording")
        trees[name] = Path(tree)
    if returned is not None:
        entry["summary"] = local_paths_out(returned["summary"], trees)
        if returned.get("questions"):
            entry["questions"] = [local_paths_out(q, trees) for q in returned["questions"]]
        if "data" in returned:
            entry["data"] = returned["data"]
    outputs, checks, objects = {}, {}, []
    outbox = Path(pending["outbox"]) if pending.get("outbox") else None
    expected = set()
    for name in pending["outputs"]:
        art = run.w.artifacts[name]
        if art["kind"] == "store":
            file = store_name(name, art)
            expected.add(file)
            content = (outbox / file).read_bytes() if outbox and (outbox / file).is_file() else None
            if content is not None:
                outputs[name] = content_hash(content)
                objects.append((content, file.rsplit(".", 1)[1]))
            checks[name] = verify(run, name, unit.ctx, outbox) if outbox else [f"{file} is missing"]
            checks[name] = checks[name] or "pass"
            continue
        if art["kind"] != "file":
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
    extra = sorted(p.name for p in outbox.iterdir() if p.name not in expected) if outbox and outbox.is_dir() else []
    if extra:
        checks["outbox"] = [f"{n} in the outbox is no output of this step" for n in extra]
    if outputs:
        entry["outputs"] = outputs
    if checks:
        entry["verify"] = checks
    name = entry_name(unit_id, pending["attempt"])
    if run.store.has(run.run, name):
        raise Fail(EXIT_REFUSED, f"{run.run}/{name} is in the ledger already; the ledger is append-only")
    times = timing(pending.get("started"), now())
    if "started" in times:
        entry |= {"started": times["started"], "ended": times["ended"]}
    check_entry({**entry, "heads": {}}, name)  # the entry itself must be valid before anything is committed
    first = (entry.get("summary") or "").strip().splitlines()
    message = f"{unit_id} attempt {pending['attempt']} (fab0 {read_version()}): {first[0] if first else 'no summary'}"
    heads = commit_work(trees, pending["starts"], message)
    if heads:
        entry["heads"] = heads
    unpushed = push_branches(run, pending["branch"], heads)
    for content, ext in objects:  # the versions first, then the entry that names them
        run.store.put(run.run, content, ext)
    run.store.append(run.run, name, entry)
    path.unlink()
    if outbox:
        shutil.rmtree(outbox, ignore_errors=True)
    pushed = push(run)
    failed = sorted(k for k, v in checks.items() if v != "pass")
    questions = len(entry.get("questions", []))
    line = end_line(
        unit_id,
        pending["attempt"],
        "not verified (" + ", ".join(failed) + ")" if failed else "verified",
        times,
        exit_code=entry.get("exit"),
        extra=f"{questions} question{'s' if questions != 1 else ''}" if questions else "",
    )
    before, after = pending.get("outside", {}), outside_state(run)
    outside = sorted(n for n in after.keys() | before.keys() if after.get(n) != before.get(n))
    return {
        "ok": True,
        "unit": unit_id,
        "attempt": pending["attempt"],
        "entry": str(run.store.fact_path(run.run, name)),
        "verified": not failed,
        "failed": failed,
        "recorded": entry,
        **times,
        "line": line,
        **({"outside": [str(workspace_root(run) / n) for n in outside]} if outside else {}),
        **pushed,
        **({"unpushed_branches": unpushed} if unpushed else {}),
    }


def record_edit(run: Run, unit: Unit, edit: dict) -> str:
    """A person's edit of a unit's outputs on its review branch (D80), recorded as an attempt by that person: the
    files are checked like an agent's outputs, and those that pass are the outputs' latest version. Returns what
    was recorded."""
    attempt = unit.history[-1]["attempt"] + 1
    latest = unit.latest
    entry: dict = {
        "step": unit.step["step"],
        "attempt": attempt,
        "executor": "person",
        "actor": edit["author"],
        "branch": run.review_branch(unit),
        "base": latest["base"],
    }
    if unit.element_id:
        entry["unit"] = unit.element_id
    if latest.get("inputs"):
        entry["inputs"] = latest["inputs"]
    entry["gate"] = {"type": "human"}
    before = run.review_copies(unit)
    changed = sorted(
        n for n in before.keys() | edit["files"].keys() if before.get(n) != content_hash(edit["files"].get(n))
    )
    entry["summary"] = f"{edit['author']} edited {', '.join(changed)} on {edit.get('pr') or entry['branch']}."
    entry["edited"] = {"commit": edit["commit"], **({"pr": edit["pr"]} if "pr" in edit else {})}
    outputs, checks, objects = {}, {}, []
    expected = set()
    with tempfile.TemporaryDirectory(prefix="fab0-edit-") as tmp:
        for file, data in edit["files"].items():
            (Path(tmp) / file).parent.mkdir(parents=True, exist_ok=True)
            (Path(tmp) / file).write_bytes(data)
        for name in unit.step["outputs"]:
            file = store_name(name, run.w.artifacts[name])
            expected.add(file)
            if file in edit["files"]:
                outputs[name] = content_hash(edit["files"][file])
                objects.append((edit["files"][file], file.rsplit(".", 1)[1]))
            checks[name] = verify(run, name, unit.ctx, Path(tmp)) or "pass"
    extra = sorted(set(edit["files"]) - expected)
    if extra:
        checks["review"] = [f"{n} in the review folder is no output of this step" for n in extra]
    if outputs:
        entry["outputs"] = outputs
    entry["verify"] = checks
    stamp = now()
    entry |= {"started": stamp, "ended": stamp}
    name = entry_name(unit.id, attempt)
    check_entry(entry, name)
    for content, ext in objects:
        run.store.put(run.run, content, ext)
    if run.store.append(run.run, name, entry) == "exists":
        raise Fail(EXIT_REFUSED, f"{run.run}/{name} is in the ledger already; the ledger is append-only")
    push(run)
    return f"recorded {edit['author']}'s edit as attempt {attempt}"


def local_paths_out(text: str, trees: dict[str, Path]) -> str:
    """An agent's text without the unit's worktree paths, which mean nothing on another machine: the ledger is
    shared by everyone who runs the project."""
    for tree in trees.values():
        text = text.replace(f"{tree}/", "")
    return text


def commit_work(trees: dict[str, Path], starts: dict, message: str) -> dict[str, str]:
    """Commit what the unit changed in each worktree, concluding a merge the session started. Returns, per
    repository, the commit its branch is at, when the branch has commits its parent lacked at the hand-out."""
    heads = {}
    for name, tree in sorted(trees.items()):

        def git(*args: str, tree: Path = tree) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True)

        git("add", "-A")
        merging = git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
        if merging or git("diff", "--cached", "--quiet").returncode:
            done = git("commit", "-q", "--no-verify", "-m", message)
            if done.returncode:
                raise Fail(EXIT_REFUSED, f"the commit in {tree} failed: {done.stderr.strip()}")
        head = git("rev-parse", "HEAD").stdout.strip()
        start = starts.get(name, {}).get("sha")
        if head and (not start or git("merge-base", "--is-ancestor", head, start).returncode):
            heads[name] = head
    return heads


def push_branches(run: Run, branch: str, heads: dict[str, str]) -> list[str]:
    """Push the unit's branch in each repository it committed to: a branch with no commits of its own stays on this
    machine (D81). A push that fails is said, not fatal: `apply` pushes the branch before it merges it or opens its
    pull request. Returns the repositories it couldn't push to."""
    from .git import Writer

    failed = []
    for name in sorted(heads):
        try:
            Writer(run.repos[name]).push(branch)
        except Fail:
            failed.append(name)
    return failed


def push(run: Run) -> dict:
    """Make the ledger visible to everyone. A push that fails is said, not fatal: the next `apply` pushes again."""
    try:
        lost = run.store.sync("push")
    except Fail as exc:
        return {"unpushed": exc.payload["error"]}
    if lost:
        names = ", ".join(f"{r}/{n}" for r, n in lost)
        return {"warnings": [f"another machine recorded {names} first; its entry stands"]}
    return {}
