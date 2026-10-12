"""A whole Story run across two repos, driven only by what `next` returns."""

from conftest import SPEC, TASKS


def test_story_from_spec_to_done(ws):
    # Spec: the product owner's stage, on the deep model, briefed with the issue body.
    res = ws.next()
    assert res["kind"] == "stage"
    assert (res["stage"], res["attempt"], res["agent"], res["model"]) == ("spec", 1, "fab0:spec-author", "opus")
    assert (res["branch"], res["start_from"], res["write"]) == ("spec/412-booking-extension", "base", ["home"])
    assert "Guests can extend a booking" in res["brief"]
    assert "questions" in res["return_schema"]["properties"]

    # Someone else can't run it.
    other = ws.next(user="backend-dev")
    assert (other["kind"], other["reason"], other["owner"]) == ("wait", "owner", "product-owner")

    ws.checkout(ws.home, res["branch"], res["start_from"])
    ws.write(ws.home, "specs/412.md", SPEC)
    ws.write(ws.home, "work/412/tasks.yaml", TASKS)
    rec = ws.record("spec", {"summary": "Wrote the spec and two tasks."})
    assert rec["verified"] and rec["entry"] == "work/412/ledger/spec-1.yaml"
    ws.commit(ws.home, "spec")

    # The human gate: no PR yet, then the open PR.
    res = ws.next()
    assert (res["kind"], res["reason"], res["reviewers"]) == ("wait", "human-gate", ["po-user"])
    assert "pr" not in res and res["refs"] == "acme/shop#412"
    pr = ws.open_pr("acme/shop", "spec/412-booking-extension")
    assert ws.next()["pr"]["number"] == pr

    # A requested change re-runs the stage on the same branch, with the review in the brief.
    ws.add_review("acme/shop", pr, "CHANGES_REQUESTED", "Say what happens when the room is taken.", review_id=1)
    res = ws.next()
    assert (res["kind"], res["stage"], res["attempt"], res["start_from"]) == ("stage", "spec", 2, "branch")
    assert "Say what happens when the room is taken." in res["brief"]
    ws.write(ws.home, "specs/412.md", SPEC + "- S2: a guest is told when the room is taken.\n")
    assert ws.record("spec", {"summary": "Added S2."})["entry"] == "work/412/ledger/spec-2.yaml"
    ws.commit(ws.home, "spec rework")
    assert ws.next()["reason"] == "human-gate"

    # Approval is merging the PR. Then the plan action is pending, for anyone.
    ws.merge("acme/shop", "spec/412-booking-extension")
    res = ws.next(user="backend-dev")
    assert (res["kind"], res["action"]) == ("action", "plan")
    ops = [(op["op"], op.get("title") or op.get("issue", {}).get("task")) for op in res["ops"]]
    assert ops == [
        ("create-issue", "[412 T1] Booking API extension"),
        ("add-sub-issue", None),
        ("create-issue", "[412 T2] Booking screen"),
        ("add-sub-issue", None),
        ("add-blocked-by", "T2"),
    ]
    assert res["ops"][0]["issue"]["repo"] == "acme/shop-api" and res["ops"][0]["assignees"] == ["backend-dev"]
    assert res["ops"][2]["assignees"] == ["po-user"]
    ws.apply_plan(res["ops"])

    # Tasks: T2 waits for T1; T1 is the backend developer's.
    res = ws.next()
    assert (res["kind"], res["reason"]) == ("wait", "tasks")
    assert {t["task"]: t["state"] for t in res["tasks"]} == {"T1": "assigned", "T2": "blocked"}
    res = ws.next(user="backend-dev")
    assert res["kind"] == "tasks"
    assert [t["task"] for t in res["runnable"]] == ["T1"] and res["lanes"] == [["T1"]]
    refused = ws.next("--task", "T1")
    assert (refused["kind"], refused["reason"]) == ("wait", "assigned")

    # T1: implement on the balanced model in the backend repo, gated by the reviewer on the deep model.
    res = ws.next("--task", "T1", user="backend-dev")
    assert (res["stage"], res["model"], res["write"], res["repo"]) == ("implement", "sonnet", ["backend"], "backend")
    assert res["branch"] == "implement/412-T1-booking-api-extension"
    assert "Booking API extension" in res["brief"] and "git -C" in res["brief"]
    ws.checkout(ws.api, res["branch"], res["start_from"])
    ws.write(ws.api, "src/booking.py", "def extend(booking):\n    return booking\n")
    ws.record("implement", {"summary": "Added extend()."}, task="T1")
    ws.commit(ws.api, "T1")
    gate = ws.next("--task", "T1", user="backend-dev")
    assert (gate["kind"], gate["agent"], gate["model"], gate["attempt"]) == ("gate", "fab0:reviewer", "opus", 1)
    assert "implement/412-T1-booking-api-extension" in gate["brief"]

    # A reject re-runs the producer with the findings.
    rec = ws.record(
        "implement",
        {
            "decision": "reject",
            "reasons": ["S1 isn't checked."],
            "findings": [{"check": "coverage", "message": "No room check", "location": "src/booking.py:1"}],
            "unmet": ["S1"],
        },
        task="T1",
        gate=True,
    )
    assert rec["entry"] == "work/412/ledger/implement-T1-1-gate.yaml" and rec["decision"] == "reject"
    ws.commit(ws.api, "gate 1")
    res = ws.next("--task", "T1", user="backend-dev")
    assert (res["kind"], res["attempt"], res["start_from"]) == ("stage", 2, "branch")
    assert "No room check" in res["brief"] and "src/booking.py:1" in res["brief"]
    ws.write(ws.api, "src/booking.py", "def extend(booking, free):\n    return booking if free else None\n")
    ws.record("implement", {"summary": "Checks the room."}, task="T1")
    ws.commit(ws.api, "T1 rework")
    assert ws.next("--task", "T1", user="backend-dev")["attempt"] == 2
    ws.record("implement", {"decision": "approve", "reasons": ["S1 met."]}, task="T1", gate=True)
    ws.commit(ws.api, "gate 2")

    # The task PR is the human gate; it closes the task issue.
    res = ws.next("--task", "T1", user="backend-dev")
    assert (res["kind"], res["reason"], res["closes"], res["github"]) == (
        "wait",
        "human-gate",
        "acme/shop-api#413",
        "acme/shop-api",
    )
    ws.open_pr("acme/shop-api", "implement/412-T1-booking-api-extension")
    ws.merge("acme/shop-api", "implement/412-T1-booking-api-extension")
    assert ws.next("--task", "T1", user="backend-dev")["kind"] == "done"

    # T2 is unblocked for its owner.
    res = ws.next()
    assert res["kind"] == "tasks" and [t["task"] for t in res["runnable"]] == ["T2"]
    ws.do_task("T2", "po-user", {"web/booking.html": "<button>Extend</button>\n"})

    # Deliver: append the review results to the spec, remove keep: run artifacts, one PR closing the Story.
    res = ws.next(user="backend-dev")
    assert (res["kind"], res["action"]) == ("action", "deliver")
    kinds = [op["op"] for op in res["ops"]]
    assert kinds == ["branch", "append", "remove", "open-pr"]
    append = res["ops"][1]
    assert append["path"] == "specs/412.md" and "approve (attempt 2)" in append["text"]
    assert res["ops"][2]["path"] == "work/412/tasks.yaml"
    assert res["ops"][3]["body"].startswith("Closes #412") and res["ops"][3]["assignees"] == ["po-user"]
    ws.apply_deliver(res["ops"])
    res = ws.next()
    assert (res["kind"], res["reason"], res["closes"]) == ("wait", "human-gate", "acme/shop#412")
    ws.merge("acme/shop", res["branch"])
    assert ws.next()["kind"] == "done"
    assert ws.next(user="backend-dev")["kind"] == "done"


def test_profile_maps_a_class_to_another_model(ws):
    res = ws.run("next", "412", "--as", "po-user", "--profile", "cheap")
    assert res["model"] == "sonnet"


def test_questions_wait_until_the_artifact_is_edited(ws):
    res = ws.next()
    ws.checkout(ws.home, res["branch"], res["start_from"])
    ws.write(ws.home, "specs/412.md", SPEC)
    ws.write(ws.home, "work/412/tasks.yaml", TASKS)
    ws.record("spec", {"summary": "Draft.", "questions": ["One night, or more?"]})
    ws.commit(ws.home, "spec draft")
    res = ws.next()
    assert (res["kind"], res["reason"], res["questions"]) == ("wait", "questions", ["One night, or more?"])
    # The owner answers by editing the spec; the stage re-runs from the edited file.
    ws.write(ws.home, "specs/412.md", SPEC + "\nOne night only.\n")
    res = ws.next()
    assert (res["kind"], res["attempt"]) == ("stage", 2)
    assert "someone changed or removed: spec" in res["brief"]


def test_failed_verification_reruns_with_the_errors(ws):
    res = ws.next()
    ws.checkout(ws.home, res["branch"], res["start_from"])
    ws.write(ws.home, "specs/412.md", "# Booking extension\n\nNo scenarios yet.\n")
    ws.write(
        ws.home,
        "work/412/tasks.yaml",
        "- {id: T1, title: A, repo: backend, files: [a], depends_on: [T2]}\n"
        "- {id: T2, title: B, repo: nowhere, files: [b], depends_on: [T1]}\n",
    )
    rec = ws.record("spec", {"summary": "Draft."})
    assert not rec["verified"]
    assert rec["verify"]["spec"] == ["specs/412.md: missing sections: Scenarios"]
    assert "T2: repo 'nowhere' is not in fab0.yaml" in " ".join(rec["verify"]["tasks"])
    assert "depends_on has a cycle" in " ".join(rec["verify"]["tasks"])
    ws.commit(ws.home, "spec draft")
    res = ws.next()
    assert (res["kind"], res["attempt"]) == ("stage", 2)
    assert "missing sections: Scenarios" in res["brief"] and "depends_on has a cycle" in res["brief"]


def test_an_invalid_return_is_recorded_as_a_failure(ws):
    res = ws.next()
    ws.checkout(ws.home, res["branch"], res["start_from"])
    ws.write(ws.home, "specs/412.md", SPEC)
    ws.write(ws.home, "work/412/tasks.yaml", TASKS)
    rec = ws.record("spec", "not json")
    assert not rec["verified"] and "return" in rec["verify"]
    ws.commit(ws.home, "spec")
    assert ws.next()["attempt"] == 2


def test_record_needs_a_hand_out(ws):
    res = ws.record("spec", {"summary": "x"}, expect=4)
    assert "run `fab0-resolve next` first" in res["error"]


def test_three_rejects_escalate_to_a_human(ws):
    ws.do_spec()
    ws.do_plan()
    reject = {"decision": "reject", "reasons": ["Not yet."]}
    for attempt in (1, 2, 3):
        res = ws.next("--task", "T1", user="backend-dev")
        assert (res["kind"], res["attempt"]) == ("stage", attempt)
        ws.checkout(ws.api, res["branch"], res["start_from"])
        ws.write(ws.api, "src/booking.py", f"# attempt {attempt}\n")
        ws.record("implement", {"summary": "Try."}, task="T1")
        ws.commit(ws.api, f"T1 {attempt}")
        assert ws.next("--task", "T1", user="backend-dev")["kind"] == "gate"
        ws.record("implement", reject, task="T1", gate=True)
        ws.commit(ws.api, f"gate {attempt}")
    res = ws.next("--task", "T1", user="backend-dev")
    assert (res["kind"], res["reason"], res["closes"]) == ("wait", "escalated", "acme/shop-api#413")
