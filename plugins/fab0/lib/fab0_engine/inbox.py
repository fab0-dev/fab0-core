"""`inbox`: every open run, and what it needs from whom. Read-only.

Open runs are found where they leave traces:
- run branches (`fab0/{run}/base`) in the home repo that aren't in its base yet;
- runs with a ledger entry in the run store (D74);
- open tracker items that match an issue trigger, started or not.

Each is evaluated like `status`, and its units are sorted for the person asking:
- `yours`: a unit that waits on a role the person has: a human gate, questions, an escalation, or the run's
  own pull request;
- `ready`: work anyone may start with `/fab0:run`: ready units and actions, auto gates to run, merges;
- `waiting`: units that wait on someone else, and units another checkout claimed (D81);
- `blocked`: units held back by a signal a person may need to act on (the tracker, an invalid dependency, a
  failure, another run's claim, an undecidable condition). Units that only wait their turn (order, data,
  claim, limit) aren't listed.

Each open run also carries `latest`, the time and line of the last event of its log on this machine, when it
has one.
"""

from __future__ import annotations

import re

from .events import events_path, read_events
from .git import Repo
from .schedule import Run, Unit
from .store import open_store

SIGNALS = ("tracker", "invalid-dependency", "failed", "cross-run", "condition", "unsupported")


def template_pattern(template: str) -> re.Pattern:
    return re.compile("^" + re.escape(template).replace(r"\{run\}", "(?P<run>[A-Za-z0-9][A-Za-z0-9._-]*)") + "$")


def candidates(wiring, tracker) -> list[str]:
    """Run ids that may be open, oldest traces first, each once."""
    found: dict[str, None] = {}
    project = wiring.project
    for name in project["pipelines"]:
        pipeline = wiring.pipelines[name]
        policy = wiring.git_policy(pipeline)
        home = Repo("home", project["repos"]["home"], wiring.root, policy["base"])
        if not home.exists():
            continue
        if policy["branching"] == "run":
            pattern = template_pattern(policy.get("branches", {}).get("run", "fab0/{run}/base"))
            base = home.fresh(home.base)
            refs = home.out("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin")
            for ref in refs.splitlines():
                m = pattern.match(ref.removeprefix("origin/"))
                if m and not (base and home.is_ancestor(home.fresh(m.group(0)), base)):
                    found.setdefault(m["run"])
        issue = pipeline["trigger"].get("issue")
        if issue and tracker is not None:
            for number in tracker.open_items(home.github, issue.get("label"), issue.get("type")):
                found.setdefault(str(number))
    store = open_store(project, wiring.root)
    for run in store.runs() if store else []:
        found.setdefault(run)
    return list(found)


def pull_of(run: Run, unit: Unit) -> str | None:
    if not unit.branch or run.tracker is None:
        return None
    if run.store_reviewed(unit):
        found = run.review_pulls(unit)
        github = run.store_github()
    else:
        repo = run.repos[unit.pr_repo]
        found = run.tracker.pulls(repo.github, unit.branch, run.parent_in(repo, unit.ctx.parent))
        github = repo.github
    pr = found[-1] if found else None
    return f"{github}#{pr['number']}" if pr and pr["state"] == "open" else None


def latest_event(run: Run) -> dict | None:
    """The time and line of the last event in the run's log on this machine, if it has one. Read-only."""
    events = read_events(events_path(run))
    return {"time": events[-1]["time"], "line": events[-1]["line"]} if events else None


def inbox(wiring, tracker, user: str, pick) -> dict:
    """`pick(run)` names a run's pipeline, or raises when it can't."""
    roles = {role for role, who in wiring.people.items() if user in who.get("github", [])}
    out: dict = {"ok": True, "user": user, "roles": sorted(roles), "runs": [], "skipped": []}
    lists: dict[str, list] = {"yours": [], "ready": [], "waiting": [], "blocked": []}
    for run_id in candidates(wiring, tracker):
        try:
            name = pick(run_id)
        except Exception as exc:  # a trace that belongs to no pipeline now: say so, don't fail the inbox
            out["skipped"].append({"run": run_id, "why": str(getattr(exc, "payload", {}).get("error", exc))})
            continue
        run = Run(wiring, name, run_id, user, tracker)
        result = run.evaluate()
        if result["kind"] == "done":
            continue
        item = run.item()
        latest = latest_event(run)
        out["runs"].append(
            {
                "run": run_id,
                "pipeline": name,
                "kind": result["kind"],
                **({"title": item["title"]} if item else {}),
                **({"latest": latest} if latest else {}),
            }
        )
        if result["kind"] == "waiting":
            owner = run.w.project["repos"]["home"].get("owner")
            entry = {"run": run_id, "unit": run.run_branch, "reason": result["reason"], "owner": owner}
            lists["yours" if owner in roles else "waiting"].append(entry)
            continue
        elsewhere = [u for u in run.units if (u.reason or {}).get("claimed") == "elsewhere"]
        ready = [
            u.id
            for u in run.units
            if u.state in ("ready", "running")
            and u.step
            and u not in elsewhere
            or u.state == "waiting"
            and (u.reason or {}).get("gate") in ("merge", "gate", "running")
        ]
        if ready:
            lists["ready"].append({"run": run_id, "pipeline": name, "units": ready, "command": f"/fab0:run {run_id}"})
        for unit in elsewhere:  # another checkout took it (D81): it is under way, on someone else's machine
            lists["waiting"].append({"run": run_id, "unit": unit.id, "reason": unit.reason, "owner": None})
        for unit in run.units:
            reason = unit.reason or {}
            if unit.state == "waiting" and reason.get("gate") not in ("merge", "gate", "running"):
                owner = run.owner(unit)
                entry = {"run": run_id, "unit": unit.id, "reason": reason, "owner": owner}
                pull = pull_of(run, unit)
                if pull:
                    entry["pull"] = pull
                lists["yours" if owner in roles else "waiting"].append(entry)
            elif unit.state == "blocked" and set(reason) & set(SIGNALS):
                lists["blocked"].append({"run": run_id, "unit": unit.id, "reason": reason})
    return {**out, **lists}
