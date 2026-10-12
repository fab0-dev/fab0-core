"""Hash staleness: a changed input re-runs what was built from it, except tasks that are already done."""

from conftest import SPEC, TASKS, git


def test_a_spec_change_makes_open_tasks_stale_and_replans(ws):
    ws.do_spec()
    ws.do_plan()
    ws.do_task("T1", "backend-dev", {"src/booking.py": "def extend(b):\n    return b\n"})
    t2 = ws.do_task("T2", "po-user", {"web/booking.html": "<button>Extend</button>\n"}, merge=False)
    ws.open_pr("acme/shop", t2["branch"])

    # The product owner changes the intent: the merged spec is stale, so spec runs again from the base.
    data = ws.gh
    data["issues"]["acme/shop#412"]["body"] += "\nUp to three nights."
    ws.save_gh(data)
    res = ws.next()
    assert (res["kind"], res["stage"], res["attempt"], res["start_from"]) == ("stage", "spec", 2, "base")
    assert "The inputs changed since attempt 1: intent" in res["brief"]

    new_spec = SPEC + "- S2: up to three nights.\n"
    new_tasks = TASKS.replace("Booking screen", "Booking screen with nights")
    ws.checkout(ws.home, res["branch"], res["start_from"])
    ws.write(ws.home, "specs/412.md", new_spec)
    ws.write(ws.home, "work/412/tasks.yaml", new_tasks)
    assert ws.record("spec", {"summary": "Up to three nights."})["verified"]
    ws.commit(ws.home, "spec 2")
    wait = ws.next()
    assert (wait["kind"], wait["reason"]) == ("wait", "human-gate")
    assert "pr" not in wait  # the earlier spec PR is merged, so this attempt needs a new one
    ws.open_pr("acme/shop", res["branch"])
    ws.merge("acme/shop", res["branch"])

    # plan reconciles the sub-issues with the changed task list.
    res = ws.next()
    assert (res["kind"], res["action"]) == ("action", "plan")
    assert [(op["op"], op.get("title")) for op in res["ops"]] == [("edit-title", "[412 T2] Booking screen with nights")]
    ws.apply_plan(res["ops"])

    # T1 is done (its PR was merged), so it stays done. T2 is open and goes stale.
    assert ws.next("--task", "T1", user="backend-dev")["kind"] == "done"
    res = ws.next("--task", "T2")
    assert (res["kind"], res["attempt"], res["start_from"]) == ("stage", 2, "branch")
    assert "The inputs changed since attempt 1: spec, tasks" in res["brief"]


def test_a_hand_edit_of_a_merged_output_reruns_its_stage(ws):
    ws.do_spec()
    ws.write(ws.home, "specs/412.md", SPEC + "\nEdited on main.\n")
    ws.commit(ws.home, "edit spec by hand")
    res = ws.next()
    assert (res["kind"], res["stage"], res["attempt"]) == ("stage", "spec", 2)
    assert "someone changed or removed: spec" in res["brief"]


def test_a_removed_output_reruns_its_stage(ws):
    ws.do_spec(merge=False)
    ws.open_pr("acme/shop", "spec/412-booking-extension")
    ws.merge("acme/shop", "spec/412-booking-extension")
    ws.do_plan()
    # tasks.yaml disappears from main without a spec run: the spec stage re-runs to restore it.
    git(ws.home, "rm", "-q", "work/412/tasks.yaml")
    ws.commit(ws.home, "remove tasks")
    res = ws.next()
    assert (res["kind"], res["stage"]) == ("stage", "spec")
    assert "someone changed or removed: tasks" in res["brief"]
