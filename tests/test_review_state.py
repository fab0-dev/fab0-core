"""A waiting stage with an agent gate says whether the attempt its branch holds was reviewed."""

REVIEW_KEYS = {"review", "draft"}
BRANCH = "implement/412-T1-booking-api-extension"


def start(ws):
    ws.do_spec()
    ws.do_plan()


def attempt(ws, files, returned=None):
    """Run the next implement attempt of T1 and commit it; returns the attempt number."""
    res = ws.next("--task", "T1", user="backend-dev")
    assert res["kind"] == "stage" and res["stage"] == "implement", res
    ws.checkout(ws.api, res["branch"], res["start_from"])
    for rel, text in files.items():
        ws.write(ws.api, rel, text)
    ws.record("implement", returned or {"summary": f"Attempt {res['attempt']}."}, task="T1")
    ws.commit(ws.api, f"attempt {res['attempt']}")
    return res["attempt"]


def gate(ws, decision, reasons=("Because.",)):
    res = ws.next("--task", "T1", user="backend-dev")
    assert res["kind"] == "gate", res
    ws.record("implement", {"decision": decision, "reasons": list(reasons)}, task="T1", gate=True)
    ws.commit(ws.api, f"gate {res['attempt']}")


def wait(ws):
    return ws.next("--task", "T1", user="backend-dev")


def test_questions_are_unreviewed_and_a_draft(ws):
    start(ws)
    n = attempt(ws, {"src/a.py": "x = 1\n"}, {"summary": "Stuck.", "questions": ["Which room?"]})
    res = wait(ws)
    assert (res["kind"], res["reason"], res["questions"]) == ("wait", "questions", ["Which room?"])
    assert res["draft"] is True
    assert res["review"] == {
        "reviewed": False,
        "attempt": n,
        "text": "Not reviewed: attempt 1 returned questions, so the review gate hasn't looked at this change. "
        "It reviews the next attempt, after the answers.",
    }
    # The gate is not handed out, whatever the pull request looks like.
    ws.open_pr("acme/shop-api", BRANCH)
    again = wait(ws)
    assert again["kind"] == "wait" and again["reason"] == "questions"
    assert again["review"] == res["review"] and again["draft"] is True


def test_human_gate_after_approve_is_reviewed(ws):
    start(ws)
    attempt(ws, {"src/a.py": "x = 1\n"})
    gate(ws, "approve")
    res = wait(ws)
    assert (res["kind"], res["reason"]) == ("wait", "human-gate")
    assert res["draft"] is False
    assert res["review"] == {
        "reviewed": True,
        "attempt": 1,
        "decision": "approve",
        "text": "Reviewed: the review gate decided approve on attempt 1.",
    }
    # Stable when the pull request exists.
    ws.open_pr("acme/shop-api", BRANCH)
    assert wait(ws)["review"] == res["review"]


def test_later_attempt_passing_the_gate_is_reviewed_after_questions(ws):
    start(ws)
    attempt(ws, {"src/a.py": "x = 1\n"}, {"summary": "Stuck.", "questions": ["Which room?"]})
    pr = ws.open_pr("acme/shop-api", BRANCH)
    ws.add_review("acme/shop-api", pr, "COMMENTED", "Any room.", review_id=1)
    assert attempt(ws, {"src/a.py": "x = 2\n"}) == 2
    gate(ws, "approve")
    res = wait(ws)
    assert (res["reason"], res["draft"]) == ("human-gate", False)
    assert res["review"]["text"] == "Reviewed: the review gate decided approve on attempt 2."
    assert res["review"]["attempt"] == 2 and res["pr"]["number"] == pr


def test_escalated_says_the_decision_on_the_last_attempt(ws):
    start(ws)
    for n in (1, 2, 3):
        assert attempt(ws, {"src/a.py": f"x = {n}\n"}) == n
        gate(ws, "reject")
    res = wait(ws)
    assert (res["kind"], res["reason"]) == ("wait", "escalated")
    assert res["draft"] is False
    assert res["review"] == {
        "reviewed": True,
        "attempt": 3,
        "decision": "reject",
        "text": "Reviewed: the review gate decided reject on attempt 3.",
    }


def test_escalate_decision_is_stated(ws):
    start(ws)
    attempt(ws, {"src/a.py": "x = 1\n"})
    gate(ws, "escalate")
    res = wait(ws)
    assert res["reason"] == "escalated" and res["review"]["decision"] == "escalate"
    assert res["review"]["text"] == "Reviewed: the review gate decided escalate on attempt 1."


def test_reviewed_pull_request_with_a_new_questions_attempt_is_unreviewed(ws):
    start(ws)
    attempt(ws, {"src/a.py": "x = 1\n"})
    gate(ws, "approve")
    pr = ws.open_pr("acme/shop-api", BRANCH)
    assert wait(ws)["draft"] is False
    ws.add_review("acme/shop-api", pr, "CHANGES_REQUESTED", "Handle the taken room.", review_id=1)
    attempt(ws, {"src/a.py": "x = 2\n"}, {"summary": "Unsure.", "questions": ["Overbook?"]})
    res = wait(ws)
    assert res["reason"] == "questions" and res["draft"] is True
    assert res["review"]["reviewed"] is False and res["review"]["attempt"] == 2
    assert "decision" not in res["review"]
    assert res["review"]["text"].startswith("Not reviewed: attempt 2 returned questions")


def test_stage_without_an_agent_gate_has_no_review_fields(ws):
    ws.do_spec(merge=False)
    res = ws.next()
    assert (res["kind"], res["reason"]) == ("wait", "human-gate")
    assert not REVIEW_KEYS & set(res)
