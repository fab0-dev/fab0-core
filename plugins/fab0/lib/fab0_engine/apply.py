"""`apply`: the engine performs what is deterministic, so no model executes git or gh recipes (D5).

One call loops until nothing more can be done:
- every action whose check fails is performed: a lifecycle action, or an action step;
- every unit whose gate allows it is merged into its parent: gate `none` directly (D28), `merge: auto` after
  approval, within `git.auto_merge` and once its pull request's required checks pass;
- every unit a person must look at gets a pull request: a draft until its gate approves, then ready. A pull
  request is never turned back into a draft (from #17), and questions and escalations are posted on it once.

Each operation is idempotent: its check comes first, so running `apply` twice changes nothing the second time.
"""

from __future__ import annotations

from .actions import check, child_titles, written
from .errors import EXIT_ERROR, Fail
from .git import Writer
from .schedule import Run, Unit
from .templates import render

ROUNDS = 10  # an apply settles in a few rounds; more means something keeps undoing itself


def perform(run: Run, unit: Unit) -> str:
    """Do one action whose check failed; returns what was done."""
    name, args = unit.step["action"], unit.step.get("with", {})
    gh, home = run.tracker, run.home.github
    if name == "tracker.child.sync":
        have = {(c["title"], c["type"]) for c in gh.sub_issues(home, int(run.run))}
        made = []
        for element, title in child_titles(run, args, unit.ctx, unit.elements or []):
            if (title, args.get("type")) in have or (not args.get("type") and title in {t for t, _ in have}):
                continue
            body = f"{element['id']} of run {run.run} ({run.name})."
            child = gh.create_issue(home, title, body, args.get("type"))
            gh.add_sub_issue(home, int(run.run), child)
            made.append(f"#{child['number']}")
        return f"created {', '.join(made)}"
    args = render(args, run.lookup(unit.ctx))
    if name == "git.run-branch":
        made = []
        for repo in written(run):
            if not repo.fresh(run.run_branch):
                Writer(repo).create_branch(run.run_branch, repo.fresh(repo.base))
                made.append(repo.name)
        return f"created {run.run_branch} in {', '.join(made)}"
    if name == "git.sync-base":
        for repo in written(run):
            result = Writer(repo).merge(repo.base, run.run_branch, f"fab0: bring {repo.base} into run {run.run}")
            if "conflict" in result:
                raise Fail(
                    EXIT_ERROR, f"{repo.name}: {repo.base} conflicts with {run.run_branch}", conflict=result["conflict"]
                )
        return f"merged the base into {run.run_branch}"
    if name == "git.remove-run-artifacts":
        removed = []
        for art_name, art in run.w.artifacts.items():
            spec = art.get("file", {})
            if art.get("keep") != "run" or "{unit}" in spec.get("path", "") or spec.get("repo", "").startswith("unit."):
                continue
            repo, path = run.artifact_location(art_name, unit.ctx)
            if Writer(repo).commit(run.run_branch, f"fab0: remove run {run.run}'s {art_name}", remove=[path]):
                removed.append(f"{repo.name}:{path}")
        return f"removed {', '.join(removed)}"
    if name in ("pr.open-run", "pr.ready"):
        return run_pulls(
            run, draft=bool(args.get("draft")) if name == "pr.open-run" else False, only_home=name == "pr.open-run"
        )
    if name == "pr.merge":
        for repo in written(run):
            result = Writer(repo).merge(run.run_branch, repo.base, f"fab0: run {run.run} ({run.name})")
            if "conflict" in result:
                raise Fail(
                    EXIT_ERROR, f"{repo.name}: {run.run_branch} conflicts with {repo.base}", conflict=result["conflict"]
                )
        return f"merged {run.run_branch} into the base"
    if name == "pr.comment":
        pr = gh.pull(home, run.run_branch, run.home.base)
        gh.comment(home, pr["number"], args["body"])
        return f"commented on #{pr['number']}"
    item = run.item()
    number = int(run.run)
    if name == "tracker.label":
        gh.edit_labels(
            home,
            number,
            [x for x in args.get("add", []) if x not in item["labels"]],
            [x for x in args.get("remove", []) if x in item["labels"]],
        )
        return "labelled"
    if name == "tracker.assign":
        gh.assign(home, number, run.w.people[args["role"]]["github"])
        return f"assigned to {args['role']}"
    if name == "tracker.comment":
        gh.comment(home, number, args["body"])
        return "commented"
    if name == "tracker.close":
        gh.close(home, number)
        return "closed"
    if name == "tracker.field":
        from .actions import field_names

        tracker = run.w.project["tracker"]
        for field, value in field_names(run, args).items():
            gh.set_field(home, number, tracker.get("owner"), tracker.get("project"), field, value)
        return "set " + ", ".join(f"{k} to {v}" for k, v in field_names(run, args).items())
    raise Fail(EXIT_ERROR, f"apply can't perform {name}")


def run_pulls(run: Run, draft: bool, only_home: bool) -> str:
    """The run's pull requests: home's from the start, and every repository whose run branch has work."""
    from .actions import ahead

    done = []
    for repo in written(run):
        if only_home and repo.name != "home":
            continue
        if not only_home and not ahead(repo, run):
            continue
        pr = run.tracker.pull(repo.github, run.run_branch, repo.base)
        if pr is None or pr["state"] == "closed":
            if not ahead(repo, run):  # GitHub refuses a pull request with nothing in it
                Writer(repo).commit(run.run_branch, f"fab0: start run {run.run} ({run.name})", allow_empty=True)
            Writer(repo).push(run.run_branch)
            title = f"{run.name} run {run.run}" + (f": {run.item()['title']}" if run.item() else "")
            closes = f"\n\nCloses #{run.run}" if repo.name == "home" and run.item() else ""
            number = run.tracker.create_pr(
                repo.github,
                run.run_branch,
                repo.base,
                title,
                f"Run {run.run} of the {run.name} pipeline.{closes}",
                draft,
            )
            done.append(f"opened {repo.github}#{number}")
        elif pr["draft"] and not draft:
            run.tracker.ready_pr(repo.github, pr["number"])
            done.append(f"marked {repo.github}#{pr['number']} ready")
    return "; ".join(done)


# ------------------------------------------------------------ units


def repos_of(run: Run, unit: Unit) -> list[str]:
    """Where a unit's branch is: the repositories a step writes, or every one that has a gated block's branch."""
    if unit.step:
        return list(dict.fromkeys(unit.write or [unit.ledger_repo]))
    return [name for name, repo in run.repos.items() if repo.exists() and repo.fresh(unit.branch)]


def merge_unit(run: Run, unit: Unit) -> str | None:
    """Merge a unit's branch into its parent in every repository it worked in, within the merge policy."""
    auto = unit.auto is not None and unit.gate.get("merge") == "auto"
    if unit.gate != "none" and not auto:
        return None
    if auto:
        into = "run" if unit.ctx.parent is not None else "base"
        policy = run.policy.get("auto_merge", {})
        if into not in policy.get("into", []) or into == "base" and not policy.get("allow_base"):
            raise Fail(EXIT_ERROR, f"{unit.id}: git.auto_merge doesn't allow merging into the {into} branch")
    merged = []
    names = repos_of(run, unit)
    for name in names:
        repo = run.repos[name]
        parent = run.parent_in(repo, unit.ctx.parent)
        if auto:
            pr = run.tracker.pull(repo.github, unit.branch, parent)
            if pr is not None and run.tracker.checks(repo.github, pr["number"]) != "pass":
                return f"waiting for {repo.github}#{pr['number']}'s required checks"
        Writer(repo).push(unit.branch)
        result = Writer(repo).merge(unit.branch, parent, f"fab0: merge {unit.id} (run {run.run})")
        if "conflict" in result:
            return f"conflicts with {parent} in {name}: {', '.join(result['conflict'])} (resolved in R3b)"
        merged.append(f"{name}:{parent}")
        if hasattr(run.tracker, "note_merged"):
            run.tracker.note_merged(repo.github, unit.branch, parent)
    return f"merged into {', '.join(merged)}"


def unit_pull(run: Run, unit: Unit) -> list[str]:
    """A pull request for a unit a person looks at, and what it must say. Draft until its gate approves."""
    reason = unit.reason or {}
    approved = unit.auto is None or (
        unit.latest and unit.gates.get(unit.attempt, {}).get("gate", {}).get("decision") == "approve"
    )
    names = repos_of(run, unit)
    done = []
    for name in names:
        repo = run.repos[name]
        parent = run.parent_in(repo, unit.ctx.parent)
        if not repo.fresh(unit.branch):
            continue
        Writer(repo).push(unit.branch)
        pr = run.tracker.pull(repo.github, unit.branch, parent)
        if pr is None or pr["state"] == "closed":
            title = f"{unit.label}: run {run.run}"
            body = (unit.latest or {}).get("summary", "") + f"\n\nUnit {unit.id} of run {run.run} ({run.name})."
            number = run.tracker.create_pr(repo.github, unit.branch, parent, title, body, draft=not approved)
            pr = {"number": number, "draft": not approved}
            done.append(f"opened {repo.github}#{number}")
        elif pr["draft"] and approved:
            run.tracker.ready_pr(repo.github, pr["number"])
            done.append(f"marked {repo.github}#{pr['number']} ready")
        note = None
        if "questions" in reason:
            questions = "\n".join(f"- {q}" for q in unit.latest.get("questions", []))
            note = (
                f"<!-- fab0:questions {unit.id} {unit.attempt} -->\nThis needs answers before it goes on:\n\n"
                f"{questions}\n\nAnswer by changing an input: the item, a review, or a file."
            )
        elif "escalated" in reason:
            reasons = (
                "\n".join(f"- {r}" for r in unit.gates.get(unit.attempt, {}).get("verdict", {}).get("reasons", []))
                or f"- {reason['escalated']}"
            )
            note = f"<!-- fab0:escalated {unit.id} {unit.attempt} -->\nThe gate handed this to a person:\n\n{reasons}"
        if note:
            marker = note.splitlines()[0]
            if not any(marker in c["body"] for c in run.tracker.comments(repo.github, pr["number"])):
                run.tracker.comment(repo.github, pr["number"], note)
                done.append(f"commented on {repo.github}#{pr['number']}")
    return done


def apply(run: Run, only: str | None = None) -> dict:
    applied: list[dict] = []
    for _ in range(ROUNDS):
        result = run.evaluate()
        progress = False
        for unit in run.units:
            if only and unit.id != only:
                continue
            reason = unit.reason or {}
            if unit.step and "action" in unit.step and unit.state == "ready":
                missing = check(run, unit.step["action"], unit.step.get("with", {}), unit.ctx, unit.elements)
                if missing is not None:
                    applied.append({"unit": unit.id, "action": unit.step["action"], "did": perform(run, unit)})
                    progress = True
            elif unit.state == "waiting" and reason.get("gate") == "merge":
                did = merge_unit(run, unit)
                if did:
                    applied.append({"unit": unit.id, "merge": did})
                    progress = progress or did.startswith("merged")
            elif (
                unit.state == "waiting"
                and unit.branch
                and (reason.get("gate") in ("human", "gate", "running") or {"questions", "escalated"} & set(reason))
            ):
                for did in unit_pull(run, unit):
                    applied.append({"unit": unit.id, "pull": did})
        if not progress:
            return {**result, "applied": applied}
        run = Run(run.w, run.name, run.run, run.actor, run.tracker)  # read everything again
    raise Fail(EXIT_ERROR, f"apply didn't settle after {ROUNDS} rounds", applied=applied)
