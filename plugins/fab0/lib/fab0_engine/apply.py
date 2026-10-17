"""`apply`: the engine performs what is deterministic, so no model executes git or gh recipes (D5).

One call loops until nothing more can be done:
- every action whose check fails is performed: a lifecycle action, or an action step;
- every unit whose gate allows it is merged into its parent: gate `none` directly (D28), `merge: auto` after
  approval, within `git.auto_merge` and once its pull request's required checks pass;
- every unit a person must look at gets a pull request: a draft until its gate approves, then ready. A pull
  request is never turned back into a draft (from #17), and questions and escalations are posted on it once;
- a unit reviewed in the runs repository (D80) gets its latest outputs on its review branch and a review pull
  request there; a person's edit on that branch is recorded as their attempt, and a merge by someone who isn't
  the gate's owner is said on the pull request once;
- what was recorded in the run store and not pushed yet is pushed (D74);
- with `tracker.fields.signal` set, the run's item and its elements' items show their state on the board.

What it did, and where the run waits on a person or is blocked, goes to the run's event log (`events`).

Each operation is idempotent: its check comes first, so running `apply` twice changes nothing the second time.
"""

from __future__ import annotations

from .actions import check, child_titles, element_items, written
from .artifacts import store_name
from .errors import EXIT_ERROR, Fail
from .events import EventLog, state_line
from .git import Writer
from .handout import record_edit
from .schedule import Run, Unit
from .templates import render

ROUNDS = 10  # an apply settles in a few rounds; more means something keeps undoing itself


def perform(run: Run, unit: Unit) -> str:
    """Do one action whose check failed; returns what was done."""
    name, args = unit.step["action"], unit.step.get("with", {})
    gh, home = run.tracker, run.home.github
    if name == "tracker.child.sync":
        return sync_children(run, unit, args)
    if name == "tracker.child.close":
        items = element_items(run, args, unit.ctx, unit.elements or [], run.children())
        closed = []
        for item in items.values():
            if item["state"] != "closed":
                gh.close(item["repo"], item["number"])
                closed.append(f"#{item['number']}")
        return f"closed {', '.join(closed)}"
    args = render(args, run.lookup(unit.ctx))
    if name == "git.run-branch":
        made = []
        for repo in written(run):
            if not repo.fresh(run.run_branch):
                Writer(repo).create_branch(run.run_branch, repo.fresh(repo.base))
                made.append(repo.name)
        return f"created {run.run_branch} in {', '.join(made)}"
    if name == "git.sync-base":
        from .actions import ahead

        for repo in written(run):
            if not ahead(repo, run):
                continue  # from R8: a run branch with no work needs no base, and a merge would make it look busy
            result = Writer(repo).merge(repo.base, run.run_branch, f"fab0: bring {repo.base} into run {run.run}")
            if "conflict" in result:
                raise Fail(
                    EXIT_ERROR, f"{repo.name}: {repo.base} conflicts with {run.run_branch}", conflict=result["conflict"]
                )
        return f"merged the base into {run.run_branch}"
    if name in ("pr.open-run", "pr.ready"):
        return run_pulls(
            run, draft=bool(args.get("draft")) if name == "pr.open-run" else False, only_home=name == "pr.open-run"
        )
    if name == "pr.merge":
        from .wiring import base_allowed

        if not base_allowed(run.policy):  # validate refuses it too; this is the engine's own check
            raise Fail(EXIT_ERROR, "pr.merge: git.auto_merge doesn't allow merging into the base branch")
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
        items[element["id"]] = {"repo": home, "number": child["number"], "id": child["id"]}
        made.append(element["id"])
    linked = []
    for element in elements:
        for dep in element["after"]:
            if dep in items and (element["id"] in made or dep in made):
                blocked, blocker = items[element["id"]], items[dep]
                gh.add_blocked_by(
                    blocked["repo"], blocked["number"], blocker["repo"], blocker["number"], blocker.get("id")
                )
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
        return list(dict.fromkeys(unit.write or [unit.pr_repo]))
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
    if unit.gate != "none" and not auto and not run.labelled(unit):  # a label decides like a merge (D54)
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
            if pr is not None and pr["state"] == "open" and pr["draft"]:
                run.tracker.ready_pr(repo.github, pr["number"])  # approved: from R6, its record shows it
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
    approved = unit.auto is None or (
        unit.latest and unit.gates.get(unit.attempt, {}).get("gate", {}).get("decision") == "approve"
    )
    names = repos_of(run, unit)
    done = []
    for name in names:
        repo = run.repos[name]
        parent = run.parent_in(repo, unit.ctx.parent)
        head, base = repo.fresh(unit.branch), repo.fresh(parent)
        if not head or base and repo.is_ancestor(head, base):
            continue  # nothing on the branch to look at: GitHub opens no pull request without a change
        Writer(repo).push(unit.branch)
        pr = run.tracker.pull(repo.github, unit.branch, parent)
        if pr is None or pr["state"] in ("closed", "merged"):  # merged at an older attempt: a new one
            title = f"{unit.label}: run {run.run}"
            body = (unit.latest or {}).get("summary", "") + f"\n\nUnit {unit.id} of run {run.run} ({run.name})."
            number = run.tracker.create_pr(repo.github, unit.branch, parent, title, body, draft=not approved)
            pr = {"number": number, "draft": not approved}
            done.append(f"opened {repo.github}#{number}")
        elif pr["draft"] and approved:
            run.tracker.ready_pr(repo.github, pr["number"])
            done.append(f"marked {repo.github}#{pr['number']} ready")
        done += notes(run, unit, repo.github, pr["number"])
    return done


def notes(run: Run, unit: Unit, github: str, number: int) -> list[str]:
    """What a pull request must say about where its unit waits, each once: questions, an escalation, or a merge
    by someone who isn't the gate's owner."""
    reason = unit.reason or {}
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
    elif "merged" in reason:
        role = run.owner(unit)
        who = ", ".join(f"@{u}" for u in run.owners(unit) or []) or "nobody yet"
        note = (
            f"<!-- fab0:merged {unit.id} {unit.attempt} -->\nMerged {reason['merged']}, so the gate doesn't pass: "
            f"only {role} ({who}) approves {unit.id}. One of them approves it with an approving review here."
        )
    if note is None:
        return []
    marker = note.splitlines()[0]
    if any(marker in c["body"] for c in run.tracker.comments(github, number)):
        return []
    run.tracker.comment(github, number, note)
    return [f"commented on {github}#{number}"]


def review_pull(run: Run, unit: Unit) -> list[str]:
    """A unit reviewed in the runs repository (D80): its latest outputs on its review branch, one commit per
    attempt, and its review pull request, opened again while the latest outputs aren't merged."""
    attempt = unit.latest["attempt"]
    files = {
        store_name(n, run.w.artifacts[n]): run.store.get(run.run, h) for n, h in unit.latest.get("outputs", {}).items()
    }
    branch, github = run.review_branch(unit), run.store_github()
    done = []
    if run.store.review(run.run, unit.id, files, f"{unit.id} attempt {attempt} (run {run.run})"):
        done.append(f"put attempt {attempt} on {branch}")
    pr = run.tracker.pull(github, branch, "main")
    if pr is None or pr["state"] == "closed" or pr["state"] == "merged" and not run.holds_latest(unit):
        role = run.owner(unit)
        body = (
            unit.latest.get("summary", "")
            + f"\n\nUnit {unit.id} of run {run.run} ({run.name}), attempt {attempt}: its outputs are in "
            f"`{run.store.review_dir(run.run, unit.id)}/`. Review them here; a review that asks for changes, or a "
            "comment, brings a new attempt, and an edit you push to this branch counts as your own attempt. "
            + (f"Merging approves them when someone in {role} merges." if role else "Merging approves them.")
        )
        number = run.tracker.create_pr(github, branch, "main", f"{unit.label}: run {run.run}", body, draft=False)
        pr = {"number": number}
        done.append(f"opened {github}#{number}")
    return done + notes(run, unit, github, pr["number"])


def publish_store(run: Run) -> str | None:
    """Push what was recorded in the run store when a record couldn't push it (D74)."""
    writes = run.store.ahead() if run.store else 0
    if not writes:
        return None
    lost = run.store.sync("push")
    said = f"pushed {writes} write{'s' if writes != 1 else ''}"
    return said + (f"; {', '.join(f'{r}/{n}' for r, n in lost)} recorded first elsewhere" if lost else "")


def describe(unit: Unit) -> str:
    """One line for the board: a unit's state, and why. The event log words its state events the same way."""
    return state_line(unit.state, unit.id, unit.reason)


def log_events(run: Run, applied: list[dict], settled: dict | None = None) -> list[str]:
    """What `apply` did, and where the run stands, in the run's event log; returns why that failed, if it did."""
    log = EventLog(run)
    log.applied(applied)
    if settled is not None:
        log.states(settled)
    return log.warnings


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


def settle(run: Run, only: str | None = None) -> tuple[Run, dict]:
    """Perform what is due, round after round. A round evaluates the run from one read of the tracker; the next
    reads it again, since the last one changed something. Returns the run as its last round evaluated it, with
    its units, and the result: that evaluation and what was applied. A caller that goes on to hand out units
    (`next --apply`) uses that evaluation, so the round costs one pass over the tracker."""
    applied: list[dict] = []
    try:
        return rounds(run, only, applied)
    except Fail as fail:  # what was done before it stopped is logged too
        warnings = log_events(run, applied) if applied and "applied" not in fail.payload else []
        if warnings:
            fail.payload["warnings"] = [*fail.payload.get("warnings", []), *warnings]
        raise


def rounds(run: Run, only: str | None, applied: list[dict]) -> tuple[Run, dict]:
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
            edit = run.review_edit(unit) if unit.state not in ("running", "done") else None
            if edit:
                applied.append({"unit": unit.id, "edit": record_edit(run, unit, edit)})
                progress = True
            elif unit.step and "action" in unit.step and unit.state == "ready":
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
                for did in (review_pull if run.store_reviewed(unit) else unit_pull)(run, unit):
                    applied.append({"unit": unit.id, "pull": did})
        if not progress:
            pushed = publish_store(run)
            if pushed:
                applied.append({"store": pushed})
            applied += [{"item": ref, "signal": text} for ref, text in signals(run, result)]
            out = {**result, "applied": applied}
            warnings = log_events(run, applied, result)  # a log that can't be written is a warning, nothing more
            if warnings:
                out["warnings"] = [*result.get("warnings", []), *warnings]
            return run, out
        run.tracker.forget()  # what changed is read again, not answered from the round before
        run = Run(run.w, run.name, run.run, run.actor, run.tracker)
    warnings = log_events(run, applied)
    raise Fail(
        EXIT_ERROR,
        f"apply didn't settle after {ROUNDS} rounds",
        applied=applied,
        **({"warnings": warnings} if warnings else {}),
    )


def apply(run: Run, only: str | None = None) -> dict:
    """What `settle` did, as `apply` prints it."""
    return settle(run, only)[1]
