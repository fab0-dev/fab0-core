"""`inbox` gives each user the same answers `next` would, across all open runs, and hands nothing out."""


def inbox(ws, user):
    return ws.run("inbox", "--as", user)


def test_inbox_follows_a_run(ws):
    data = ws.gh
    data["issues"]["acme/shop#500"] = {"title": "A bug", "type": "Bug", "state": "open"}
    data["issues"]["acme/shop#409"] = {"title": "Invoices", "type": "Story", "state": "closed"}
    ws.save_gh(data)

    res = inbox(ws, "po-user")
    assert [(r["run"], r["stage"], r["command"]) for r in res["ready"]] == [("412", "spec", "/fab0:run 412")]
    res = inbox(ws, "backend-dev")
    assert res["ready"] == [] and res["waiting"][0]["reason"].startswith("spec is run by product-owner")
    assert not (ws.home / ".git" / "fab0" / "pending").exists()

    ws.do_spec()
    res = inbox(ws, "backend-dev")
    assert [(a["run"], a["action"]) for a in res["actions"]] == [("412", "plan")]

    ws.do_plan()
    res = inbox(ws, "backend-dev")
    assert [(r["command"], r["task_title"]) for r in res["ready"]] == [
        ("/fab0:run 412 --task T1", "Booking API extension")
    ]
    res = inbox(ws, "po-user")
    assert res["ready"] == []
    assert {w["task"]: w["reason"] for w in res["waiting"]} == {
        "T1": "T1 is assigned to backend-dev; pass --take to claim it",
        "T2": "T2 is blocked by T1",
    }


def test_a_recorded_stage_without_a_pr_is_ready_to_hand_off(ws):
    ws.do_spec(merge=False)
    res = inbox(ws, "po-user")
    assert res["ready"] == [
        {
            "run": "412",
            "type": "Story",
            "title": "Booking extension",
            "command": "/fab0:run 412",
            "stage": "spec",
            "task": None,
            "note": "open its PR",
        }
    ]
    pr = ws.open_pr("acme/shop", "spec/412-booking-extension")
    res = inbox(ws, "po-user")
    assert res["ready"] == [] and f"pull/{pr}" in res["waiting"][0]["reason"]


def test_text_inputs_are_inlined_in_the_brief(ws):
    ws.do_spec()
    ws.do_plan()
    res = ws.next("--task", "T1", user="backend-dev")
    assert "~~~~markdown\n# Booking extension" in res["brief"]
    assert "S1: a guest extends a booking" in res["brief"]
    assert res["workspace"]["backend"]["path"] == str(ws.api.resolve())
