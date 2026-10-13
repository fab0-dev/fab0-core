---
name: run
description: Run a fab0 pipeline for one work item (a run) until it needs a person, is done, or is blocked. The resolver decides every step; this procedure only executes it.
argument-hint: <run> [--task T] [--take] [--profile P]
disable-model-invocation: true
---

# /fab0:run $ARGUMENTS

You are the fab0 orchestrator for this session. **You execute; you don't decide.** `fab0-resolve` (on the Bash `PATH`) decides what happens next. Don't skip, reorder or improvise steps, and don't do a stage's work yourself: agents do it. All GitHub writes follow the `fab0:github` skill; load it now with the Skill tool.

Arguments: the first is the run id (an issue number). The options are:
- `--task T`: work only on task T;
- `--take`: claim task T if someone else has it;
- `--profile P`: apply profile P to `fab0.yaml`.

Pass `--task` and `--profile` on to every `fab0-resolve next` call.

## Before the loop

1. `fab0-resolve validate`. If it fails, show the errors and stop.
2. For each repository in its `workspace` with `exists: true`, run `git -C <path> fetch --prune origin`. If a repository doesn't exist locally, say so. The run may still work if the next steps don't touch it.

## The loop

Repeat until a step says stop:

1. Run `fab0-resolve next <run> [--task T] [--profile P]` and read the JSON. Its `workspace` maps repository names to local paths.
2. Act on `kind`:

**`stage`**: an agent stage to run.
1. Prepare the branch in every repository listed in `write`:
   - If the working tree has changes the person made, stop and ask. Never stash or discard them.
   - If `start_from` is `branch`, run `git checkout <branch>`. If the branch exists only on origin, use `git checkout -b <branch> --track origin/<branch>`.
   - If `start_from` is `base`, run `git checkout -B <branch> origin/<base>`.
2. Call the Agent tool with `subagent_type` = `agent`, `model` = `model`, and `prompt` = `brief`, verbatim. Don't add to the brief or summarise it.
3. Take the JSON object from the agent's final message. If the message is wrapped in a code fence, take its content. Write it to `<home git dir>/fab0/returns/<run>-<stage>[-<task>].json`; `git rev-parse --git-dir` in the home repo gives the git dir. Never write it inside a working tree: that would break the write scope.
4. Run `fab0-resolve record <run> <stage> [--task T] --return <file>`.
   - **Exit 4** (refused): show the `violations` and stop. The person decides how to undo them. Then the same `record` can be run again.
   - **`verified: false`**: say so. The next `next` call hands the stage out again with the errors in its brief.
5. Commit in every repository in `write`. The subject names the release that handed out this attempt: `<fab0_version>` is the `fab0_version` field of the `next` result from step 1 of the loop, so the stage pull request's commit list shows the release of every attempt:
   ```bash
   git -C <path> add -A && git -C <path> commit -m "<stage>[ <task>] attempt <attempt> (fab0 <fab0_version>): <first line of the summary>"
   ```
6. Update the board (the `fab0:github` skill), then continue the loop.

**`gate`**: an agent gate to run.
1. Call the Agent tool with `subagent_type` = `agent`, `model` = `model`, `prompt` = `brief`.
2. Save the JSON as in the stage case, with a `-gate` suffix on the file name.
3. Run `fab0-resolve record <run> <stage> [--task T] --gate --return <file>`. If it is refused because the review is invalid, call the gate once more with the errors added after the brief. If it is refused again, stop.
4. Commit the new ledger entry in the gate's repository (`repo`): `… commit -m "<stage>[ <task>] gate <attempt>: <decision>"`. Then continue the loop.

**`action`**: apply `ops` exactly as the `fab0:github` skill says, in order. Then continue the loop. Actions belong to no one, so anyone may run them.

**`tasks`**: the run is at its tasks, and `runnable` lists the ones this person can run. Without `--task`, work through `runnable` in order: run this same loop with `--task <id>` for each until it stops. Then run `next` without `--task` again. `lanes` says which tasks are independent; run them one after another for now.

**`wait`**: act on `reason`, then stop.
- `human-gate`, `questions`, `escalated`: push and open or update the pull request, post the questions, and post the handoff comment, all as the `fab0:github` skill says. Tell the person who reviews next, and give the link.
  - When the result carries `review` and `draft` (a stage with an agent gate), the pull request and the handoff comment state `review.text` verbatim, and the pull request is a draft exactly when `draft` is true: opened as a draft, marked ready, or turned back into a draft as needed. Don't write the sentence or pick the state yourself.
  - With reason `questions` the gate is not handed out: the pull request is opened as an unreviewed draft, and the questions are posted and the item assigned to the owner, in this same session. A later attempt that the gate decides on makes the same pull request ready and updates its statement.
  - Running the same wait again with no new attempt, review or input change leaves the pull request's draft state and body as they are, and no step fails because the state is already set.
  - If GitHub refuses the draft state, still open or update the pull request with the statement, still post the questions and the handoff comment, and say in the report that the pull request couldn't be made a draft.
- `assigned`: with `--take`, claim the task (`fab0:github`) and continue the loop. Without it, say who has the task and that `--take` claims it.
- `owner`: say who owns the stage. This person can't run it.
- `tasks`: list what each task is waiting for.

**`done`**: update the board (Stage `done`), say that the run (or the task) is done, and stop.

**`blocked`**: update the board's Waiting on field with the reason, show the reason, and stop.

## At the end

Report briefly:
- what ran, with each stage, task, attempt, model, and verified or not;
- what was opened or updated on GitHub, with links, and for a pull request with a review statement whether it is a draft or ready and what the statement says;
- any pull request that couldn't be made a draft;
- what the run waits for now, and on whom.

Then stop. Don't start other runs.
