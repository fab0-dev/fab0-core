"""The checks of fab0's built-in actions: read-only, deterministic, and the record of an action.

An action is done when its check passes: its result is a fact in git or the tracker, so it writes no ledger
entry and running it twice does no harm. `apply` (step R3) performs an action whose check fails; this module
only reads. Each check returns `None` when the action is done, or what is still missing.
"""

from __future__ import annotations

from .templates import render

RUN_LEVEL = ("git.run-branch", "git.sync-base", "git.remove-run-artifacts", "pr.open-run", "pr.ready", "pr.merge")
FOREACH_ONLY = ("tracker.child.sync",)


def written(run) -> list:
    """The repositories the run may write, which have a checkout: home, and every one a step writes, or all of
    them when a step writes the repository a foreach element names."""
    names = {"home"}
    for step in run._steps(run.p["steps"]):
        targets = step.get("repos", {}).get("write", [])
        if any(t.startswith("unit.") for t in targets):
            names = set(run.repos)
            break
        names |= set(targets)
    return [run.repos[n] for n in run.repos if n in names and run.repos[n].exists()]


def ahead(repo, run) -> bool:
    """The run branch has work the base doesn't."""
    head, base = repo.fresh(run.run_branch), repo.fresh(repo.base)
    return bool(head and base) and not repo.is_ancestor(head, base)


def field_names(run, args: dict) -> dict:
    names = run.w.project.get("tracker", {}).get("fields", {})
    return {names.get(k, k): v for k, v in args.items()}


def check(run, name: str, args: dict, ctx, elements: list | None = None) -> str | None:
    """None when the action's result is in place, otherwise what is missing."""
    lookup = run.lookup(ctx)
    if name == "tracker.child.sync":
        return check_children(run, args, ctx, elements or [])
    args = render(args, lookup)
    if name.startswith(("git.", "pr.")) and run.run_branch is None:
        return None  # flat branching: no run branch, no run pull request
    if name == "git.run-branch":
        missing = [r.name for r in written(run) if not r.fresh(run.run_branch)]
        return f"no {run.run_branch} in {', '.join(missing)}" if missing else None
    if name == "git.sync-base":
        # a run branch already merged into the base needs no syncing
        behind = [
            r.name
            for r in written(run)
            if ahead(r, run) and not r.is_ancestor(r.fresh(r.base), r.fresh(run.run_branch))
        ]
        return f"{run.run_branch} lacks the latest base in {', '.join(behind)}" if behind else None
    if name == "git.remove-run-artifacts":
        left = []
        for art_name, art in run.w.artifacts.items():
            spec = art.get("file", {})
            if art.get("keep") != "run" or "{unit}" in spec.get("path", "") or spec.get("repo", "").startswith("unit."):
                continue
            repo, path = run.artifact_location(art_name, ctx)
            ref = repo.fresh(run.run_branch)
            if ref and (repo.read_file(ref, path) is not None or repo.names(ref, path)):
                left.append(f"{repo.name}:{path}")
        return f"still there: {', '.join(left)}" if left else None
    if name in ("pr.open-run", "pr.ready", "pr.merge"):
        repos = [run.home] if name == "pr.open-run" else [r for r in written(run) if ahead(r, run)]
        missing = []
        for repo in repos:
            if name == "pr.merge":
                missing.append(repo.name)  # ahead of the base: not merged
                continue
            pr = run.tracker.pull(repo.github, run.run_branch, repo.base)
            if pr is None or pr["state"] == "closed":
                missing.append(f"{repo.name}: no pull request")
            elif name == "pr.open-run" and args.get("draft") and not pr["draft"] and pr["state"] == "open":
                continue  # already marked ready: a draft isn't forced back
            elif name == "pr.ready" and pr["draft"]:
                missing.append(f"{repo.name}: a draft")
        return "; ".join(missing) or None
    if name == "pr.comment":
        pr = run.tracker.pull(run.home.github, run.run_branch, run.home.base)
        if pr is None:
            return "no run pull request to comment on"
        found = any(args["body"] in c["body"] for c in run.tracker.comments(run.home.github, pr["number"]))
        return None if found else "the comment isn't there"
    item = run.item()
    if item is None:
        return "the run has no tracker item"
    if name == "tracker.label":
        missing = [x for x in args.get("add", []) if x not in item["labels"]]
        extra = [x for x in args.get("remove", []) if x in item["labels"]]
        parts = ([f"add {', '.join(missing)}"] if missing else []) + ([f"remove {', '.join(extra)}"] if extra else [])
        return "; ".join(parts) or None
    if name == "tracker.assign":
        users = run.w.people.get(args["role"], {}).get("github", [])
        return None if set(users) & set(item["assignees"]) else f"not assigned to {args['role']}"
    if name == "tracker.comment":
        found = any(args["body"] in c["body"] for c in run.tracker.comments(run.home.github, int(run.run)))
        return None if found else "the comment isn't there"
    if name == "tracker.close":
        return None if item["state"] == "closed" else "the item is open"
    if name == "tracker.field":
        tracker = run.w.project["tracker"]
        have = run.tracker.fields(run.home.github, int(run.run), tracker.get("owner"), tracker.get("project"))
        wrong = [f"{k} is {have.get(k)!r}, not {v!r}" for k, v in field_names(run, args).items() if have.get(k) != v]
        return "; ".join(wrong) or None
    raise ValueError(f"no check for action {name}")


def child_titles(run, args: dict, ctx, elements: list) -> list[tuple[dict, str]]:
    from .schedule import Ctx

    out = []
    for element in elements:
        inner = Ctx(parent=ctx.parent, prefix=ctx.prefix, element=element)
        out.append((element, render(args.get("title", "{unit.title}"), run.lookup(inner))))
    return out


def check_children(run, args: dict, ctx, elements: list) -> str | None:
    """Every element has a sub-issue of the run's item with its title (and type, if given)."""
    item = run.item()
    if item is None:
        return "the run has no tracker item"
    children = run.tracker.sub_issues(run.home.github, int(run.run))
    missing = []
    for element, title in child_titles(run, args, ctx, elements):
        found = [c for c in children if c["title"] == title and (not args.get("type") or c["type"] == args["type"])]
        if not found:
            missing.append(element["id"])
    return f"no sub-issue for {', '.join(missing)}" if missing else None
