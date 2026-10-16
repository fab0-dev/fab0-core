"""An evaluation makes its independent tracker reads at the same time, and changes nothing else: its units, states,
reasons and order are what a sequential evaluation gives, it reads only what the evaluation uses, and at most four
requests are out at once."""

import threading
import time

import pytest
from conftest import in_flight
from fab0_engine import schedule
from fab0_engine.errors import Fail
from fab0_engine.schedule import Run
from fab0_engine.tracker import GhFake, Memo
from fab0_engine.wiring import Wiring
from test_gates import record_gate, verdict
from test_run import do_spec, finish, handouts, start_work, states
from test_tracker import REVIEW, asks, no_repeats, two_task_run

IDS = ("T1", "T2", "T3", "T4", "T5")
FIVE_TASKS = "".join(f"- {{id: {i}, title: Task {i}, repo: backend, files: [src/{i}.py]}}\n" for i in IDS)
REPO, BASE = "acme/shop-api", "fab0/412/base"


def five_units(f, ids=IDS, review: bool = True) -> None:
    """A Story run whose spec is merged, with five independent tasks, each implemented (it has its record) and
    each with an open pull request that has a review: five pull requests to read, and the run's item."""
    do_spec(f, tasks=FIVE_TASKS)
    for unit in ids:
        h = handouts(f.run("next", "412", "--unit", f"implement-{unit}"))[f"implement-{unit}"]
        finish(f, "412", h, start_work(f, h), {("backend", f"src/{unit}.py"): "x = 1\n"}, {"summary": "Implemented."})

    def pulls(data):
        for n, unit in enumerate(ids, start=2):
            data["pulls"].setdefault(REPO, []).append(
                {
                    "number": n,
                    "branch": f"fab0/412/implement-{unit}",
                    "base": BASE,
                    "reviews": [REVIEW] if review else [],
                }
            )

    f.gh_edit(pulls)
    f.reset_calls()


def run_in_process(f) -> Run:
    """The engine's own objects on the fixture, with a tracker that logs to the same calls file."""
    tracker = Memo(GhFake(str(f.gh_path)), boards=True)
    return Run(Wiring(f.home), "story", "412", "po-user", tracker)


def without_scout(monkeypatch) -> None:
    """The evaluation as it was: every read made when the walk gets to it."""
    monkeypatch.setattr(schedule.Scout, "scout", lambda self: None)


def span(calls: list[dict]) -> float:
    return max(c["end"] for c in calls) - min(c["start"] for c in calls)


# ------------------------------------------------------------ S4: independent reads overlap


def test_independent_reads_overlap_and_never_exceed_four(story):
    five_units(story)
    story.delay(0.3)
    res = story.run("status", "412")
    assert states(res)["implement-T1"] == ("ready", {"rework": ["stale"]}), "the review is news to it"
    calls = story.calls()
    pulls = [c for c in calls if c["method"] == "pulls"]
    assert len(pulls) == 5, "one read of each unit's pull request"
    assert 2 <= in_flight(calls) <= 4
    assert span(pulls) < 0.75 * sum(c["end"] - c["start"] for c in pulls), "they didn't wait for one another"
    no_repeats(calls)


def test_the_run_of_s1_reads_what_the_walk_needs_and_no_more(story):
    """In the run of S1 the first task is sent back (the review is news to it), so the second is never reached: the
    reads that remain depend on one another (its pull request is read once the spec is known to stand), and none
    is made ahead of the walk."""
    two_task_run(story)
    story.delay(0.3)
    res = story.run("status", "412")
    assert states(res)["implement-T1"] == ("ready", {"rework": ["stale"]})
    calls = story.calls()
    assert [(c["method"], c["args"][0]) for c in calls] == [("item", "acme/shop"), ("pulls", "acme/shop-api")]
    assert in_flight(calls) == 1


@pytest.mark.parametrize("command", ["next", "apply", "inbox"])
def test_every_command_reads_at_the_same_time_and_within_the_limit(story, command):
    five_units(story)
    story.delay(0.2)
    story.run(command, *([] if command == "inbox" else ["412"]), *(["--as", "po-user"] if command == "inbox" else []))
    calls = story.calls()
    assert 2 <= in_flight(calls) <= 4
    no_repeats(calls)


def test_the_evaluation_ends_with_no_thread_left_reading(story):
    five_units(story)
    story.delay(0.1)
    before = threading.active_count()
    run_in_process(story).evaluate()
    assert threading.active_count() == before
    assert len(story.calls()) >= 6


# ------------------------------------------------------------ S5: nothing but the time changes


def settled(run: Run) -> dict:
    return run.evaluate()


def evaluations(f, monkeypatch) -> tuple[dict, list[tuple], dict, list[tuple]]:
    """The result and the reads of one evaluation with the scout, and of one without."""
    f.reset_calls()
    with_scout = settled(run_in_process(f))
    reads_with = sorted(map(str, asks(f.calls())))
    f.reset_calls()
    with monkeypatch.context() as m:
        without_scout(m)
        without = settled(run_in_process(f))
    return with_scout, reads_with, without, sorted(map(str, asks(f.calls())))


def test_the_units_states_and_order_are_what_a_sequential_evaluation_gives(story, monkeypatch):
    five_units(story)
    story.run("apply", "412")  # opens the pull requests; ready ones are read the same
    result, seen, plain, seen_plain = evaluations(story, monkeypatch)
    assert result == plain
    assert [u["unit"] for u in result["units"]] == [u["unit"] for u in plain["units"]]
    assert seen == seen_plain, "only the reads the evaluation makes anyway, each once"


def test_a_review_changes_the_unit_whichever_way_it_is_read(story, monkeypatch):
    five_units(story)
    first = settled(run_in_process(story))
    story.gh_edit(lambda d: d["pulls"][REPO][2].update(reviews=[]))
    result, seen, plain, seen_plain = evaluations(story, monkeypatch)
    assert result == plain != first
    assert seen == seen_plain
    assert states(result)["implement-T3"] != states(first)["implement-T3"]


def test_a_merged_unit_and_the_ones_waiting_on_it(story, monkeypatch):
    five_units(story, review=False)
    record_gate(story, "412", "implement-T1", verdict("approve"))
    story.run("apply", "412")
    result, seen, plain, seen_plain = evaluations(story, monkeypatch)
    assert states(result)["implement-T1"] == ("done", None)
    assert result == plain and seen == seen_plain


def reads_of(f, monkeypatch) -> tuple[dict, list[str]]:
    result, seen, plain, seen_plain = evaluations(f, monkeypatch)
    assert result == plain
    assert seen == seen_plain, "only the reads the evaluation makes anyway, each once"
    return result, seen


def test_units_the_evaluation_never_reaches_are_not_read_after_the_spec_goes_stale(story, monkeypatch):
    """The spec's tasks are recorded, then the run's item changes: the spec step is no longer done, and the
    evaluation stops there. A scout that assumed it done would read all five pull requests."""
    five_units(story)
    story.gh_edit(lambda d: d["issues"]["acme/shop#412"].update(body="Guests extend a booking, or cancel it."))
    result, seen = reads_of(story, monkeypatch)
    assert states(result)["spec"] == ("ready", {"rework": ["stale"]})
    assert not any(c.startswith("('pulls'") for c in seen), seen


def test_a_unit_that_is_not_done_holds_back_the_ones_that_wait_for_it(story, monkeypatch):
    """T2 comes after T1: while T1 is sent back, nothing of T2 is read."""
    two_task_run(story)
    result, seen = reads_of(story, monkeypatch)
    assert states(result)["implement-T2"][0] == "blocked"
    assert not any(c.startswith("('pulls'") and "implement-T2" in c for c in seen), seen


def test_the_stale_evaluation_makes_no_call_the_sequential_one_would_not(story, monkeypatch):
    five_units(story)
    story.gh_edit(lambda d: d["issues"]["acme/shop#412"].update(body="Changed."))
    story.reset_calls()
    run = run_in_process(story)
    before = threading.active_count()
    run.evaluate()
    with_scout = len(story.calls())
    assert threading.active_count() == before, "nothing the evaluation didn't use is awaited"
    story.reset_calls()
    without_scout(monkeypatch)
    run_in_process(story).evaluate()
    assert with_scout == len(story.calls())


def test_the_required_checks_of_units_waiting_to_merge_are_read_with_their_pull_requests(story, monkeypatch):
    """A second wave: a unit's checks are asked for once its pull request is known."""
    five_units(story, review=False)
    for unit in ("T1", "T2", "T3"):
        record_gate(story, "412", f"implement-{unit}", verdict("approve"))
    story.gh_edit(lambda d: [p.update(checks="pending") for p in d["pulls"][REPO]])
    story.reset_calls()
    story.delay(0.2)
    res = story.run("status", "412")
    assert [states(res)[f"implement-T{n}"] for n in (1, 2, 3)] == [("waiting", {"gate": "merge"})] * 3
    calls = story.calls()
    assert [c["method"] for c in calls].count("checks") == 3
    # the unit pull-request reads still going hold some of the four slots, so how many checks overlap depends on
    # the scheduling; that they are asked for together, not one after another, does not
    assert 2 <= in_flight([c for c in calls if c["method"] == "checks"]) <= 3, "the checks were asked for together"
    no_repeats(calls)
    story.delay(0)
    result, seen, plain, seen_plain = evaluations(story, monkeypatch)
    assert result == plain and seen == seen_plain


def test_a_run_before_its_tasks_reads_nothing_it_has_no_use_for(story, monkeypatch):
    story.run("apply", "412")
    result, seen, plain, seen_plain = evaluations(story, monkeypatch)
    assert result == plain and seen == seen_plain
    assert not any(c.startswith("('pulls'") and "implement" in c for c in seen)


def test_the_evaluation_meets_a_failing_read_as_it_always_did(story, monkeypatch):
    five_units(story)

    def failing(self, repo, branch, base):
        raise Fail(1, f"gh failed on {branch}")

    def attempt():
        run = run_in_process(story)
        monkeypatch.setattr(run.tracker.backend, "pulls", lambda *a: failing(None, *a))
        with pytest.raises(Fail) as raised:
            run.evaluate()
        return raised.value.payload

    before = threading.active_count()
    with_scout = attempt()
    assert threading.active_count() == before, "no thread is left behind"
    without_scout(monkeypatch)
    assert attempt() == with_scout


# ------------------------------------------------------------ gather and the places that use it


def test_gather_gives_results_in_order_and_raises_the_first_failure_in_order(story):
    run = run_in_process(story)
    made = []

    def call(n, fail=False):
        time.sleep(0.05 * (3 - n))  # the last one finishes first
        made.append(n)
        if fail:
            raise ValueError(n)
        return n

    assert run.gather([lambda n=n: call(n) for n in range(3)]) == [0, 1, 2]
    made.clear()
    with pytest.raises(ValueError, match="1"):
        run.gather([lambda: call(0), lambda: call(1, True), lambda: call(2, True)])
    assert sorted(made) == [0, 1, 2], "every call was made"
    assert run.gather([]) == [] and run.gather([lambda: 7]) == [7]


def test_the_pull_requests_of_several_repositories_are_read_together(story):
    """`pr.ready` asks after the run's pull request in every repository its branch is ahead in."""
    five_units(story, ids=("T1",))
    story.merge("backend", "fab0/412/implement-T1", BASE)

    def drafts(data):
        for repo, number in (("acme/shop", 9), (REPO, 8)):
            data["pulls"].setdefault(repo, []).append({"number": number, "branch": BASE, "base": "main", "draft": True})

    story.gh_edit(drafts)
    story.delay(0.3)
    run = run_in_process(story)
    missing = schedule.check(run, "pr.ready", {}, schedule.Ctx(parent=run.run_branch))
    assert missing == "home: a draft; backend: a draft"
    asked = [c for c in story.calls() if c["method"] == "pulls"]
    assert sorted(c["args"][0] for c in asked) == ["acme/shop", REPO]
    assert in_flight(asked) == 2


def test_the_inputs_from_the_tracker_hash_as_they_did_one_by_one(story):
    story.edit("steps/implement.yaml", lambda d: d["inputs"].append("intent"))
    five_units(story, ids=("T1",))
    run = run_in_process(story)
    run.evaluate()
    unit = next(u for u in run.units if u.id == "implement-T1")
    from fab0_engine.artifacts import input_hashes, view

    one_by_one = {}
    for name in unit.step["inputs"]:
        h = view(run, name, unit.ctx, unit).hash
        if h is not None:
            one_by_one["unit" if name.startswith("unit") else name] = h
    assert list(input_hashes(run, unit)) == list(one_by_one)
    assert input_hashes(run, unit) == one_by_one
    assert set(one_by_one) == {"spec", "unit", "reviews", "intent"}
