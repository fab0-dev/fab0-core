"""`apply`: the engine performs what is deterministic, so no model executes git or gh recipes (D5).

One call loops until nothing more can be done:
- every action whose check fails is performed: a lifecycle action, or an action step;
- every unit whose gate allows it is merged into its parent: gate `none` directly (D28), `merge: auto` after
  approval, within `git.auto_merge` and once its pull request's required checks pass;
- every unit a person must look at gets a pull request: a draft until its gate approves, then ready. A pull
  request is never turned back into a draft (from #17), and questions and escalations are posted on it once;
- with `tracker.fields.signal` set, the run's item and its elements' items show their state on the board.

Each operation is idempotent: its check comes first, so running `apply` twice changes nothing the second time.
"""

from __future__ import annotations

from .actions import check, child_titles, element_items, written
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
        return sync_children(run, unit, args)
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


def sync_children(run: Run, unit: Unit, args: dict) -> str:
    """A sub-issue of the run's item for every element that has none, then each new item's `after` as
    blocked-by relations, both ways. Relations between items that already existed are left alone: from their
    creation, the tracker is the truth (D49)."""
    gh, home = run.tracker, run.home.github
    elements = unit.elements or []
    items = element_items(run, args, unit.ctx, elements)
    made = []
    for element, title in child_titles(run, args, unit.ctx, elements):
        if element["id"] in items:
            continue
        body = f"{element['id']} of run {run.run} ({run.name})."
        child = gh.create_issue(home, title, body, args.get("type"))
        gh.add_sub_issue(home, int(run.run), child)
        items[element["id"]] = {"repo": home, "number": child["number"]}
        made.append(element["id"])
    linked = []
    for element in elements:
        for dep in element["after"]:
            if dep in items and (element["id"] in made or dep in made):
                blocked, blocker = items[element["id"]], items[dep]
                gh.add_blocked_by(blocked["repo"], blocked["number"], blocker["repo"], blocker["number"])
                linked.append(f"#{blocked['number']} after #{blocker['number']}")
    did = f"created {', '.join('#' + str(items[i]['number']) for i in made)}"
    return did + (f"; {', '.join(linked)}" if linked else "")


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


def sync(run: Run, unit: Unit, name: str) -> str | None:
    """Bring a reworked unit's branch up to date with its parent, when that merges cleanly and its checkout,
    if any, is clean; otherwise the session merges and the agent resolves (D45)."""
    repo = run.repos[name]
    parent = run.parent_in(repo, unit.ctx.parent)
    head, base = repo.fresh(unit.branch), repo.fresh(parent)
    if not head or not base or repo.is_ancestor(base, head) or repo.merge_conflicts(head, base):
        return None
    writer = Writer(repo)
    tree = writer.checkout_of(unit.branch)
    if tree is not None and not writer.clean(tree):
        return None
    writer.merge(parent, unit.branch, f"fab0: bring {parent} into {unit.id}")
    return f"merged {parent} into {unit.branch} in {name}"


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
            if pr is not None and run.tracker.checks(repo.github, pr["number"])[0] != "pass":
                return f"waiting for {repo.github}#{pr['number']}'s required checks"
        Writer(repo).push(unit.branch)
        result = Writer(repo).merge(unit.branch, parent, f"fab0: merge {unit.id} (run {run.run})")
        if "conflict" in result:
            return f"conflicts with {parent} in {name}: {', '.join(result['conflict'])}"
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


def describe(unit: Unit) -> str:
    """One line for the board: a unit's state, and why."""
    parts = []
    for key, value in (unit.reason or {}).items():
        if isinstance(value, list):
            value = ", ".join(map(str, value))
        elif isinstance(value, dict):
            value = ", ".join(f"{k} {v}" for k, v in value.items())
        parts.append(f"{key} {value}")
    return f"{unit.state}: {unit.id}" + (f" ({'; '.join(parts)})" if parts else "")


def signals(run: Run, result: dict) -> list[tuple[str, str]]:
    """Set the signal field of the run's item and of each element's item to where it is, if it changed:
    `done`, or the first of its units that isn't. Projected, never read back (P3)."""
    tracker = run.w.project.get("tracker", {})
    field = tracker.get("fields", {}).get("signal")
    if not field or run.item() is None:
        return []
    groups: dict[str, list[Unit]] = {run.item_ref(): run.units}
    for unit in run.units:
        item = (unit.ctx.element or {}).get("item")
        if item:
            groups.setdefault(item, []).append(unit)
    changed = []
    for ref, units in groups.items():
        left = [u for u in units if u.state != "done"]
        if ref == run.item_ref() and result["kind"] != "units":
            text = "done" if result["kind"] == "done" else "waiting: the run's pull requests"
        else:
            text = describe(left[0]) if left else "done"
        repo, number = ref.split("#")
        have = run.tracker.fields(repo, int(number), tracker.get("owner"), tracker.get("project"))
        if have.get(field) != text:
            run.tracker.set_field(repo, int(number), tracker.get("owner"), tracker.get("project"), field, text)
            changed.append((ref, text))
    return changed


def apply(run: Run, only: str | None = None) -> dict:
    applied: list[dict] = []
    for _ in range(ROUNDS):
        result = run.evaluate()
        progress = False
        for unit in run.units:
            if only and unit.id != only:
                continue
            reason = unit.reason or {}
            if unit.state == "ready" and unit.rework and unit.branch:
                for name in repos_of(run, unit):
                    synced = sync(run, unit, name)
                    if synced:
                        applied.append({"unit": unit.id, "sync": synced})
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
            applied += [{"item": ref, "signal": text} for ref, text in signals(run, result)]
            return {**result, "applied": applied}
        run = Run(run.w, run.name, run.run, run.actor, run.tracker)  # read everything again
    raise Fail(EXIT_ERROR, f"apply didn't settle after {ROUNDS} rounds", applied=applied)
