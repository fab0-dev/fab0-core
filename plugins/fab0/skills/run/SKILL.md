---
name: run
description: Run a fab0 pipeline for one run until it needs a person, is blocked, or is done. The engine decides every step; this procedure only executes what it hands out.
argument-hint: <run> [--pipeline P] [--profile P] [--unit U]
disable-model-invocation: true
---

# /fab0:run $ARGUMENTS

You execute; you don't decide. `fab0-resolve` (on the Bash `PATH`) says what is due, and its `apply` command does every git and tracker write that can be done exactly. Don't skip, reorder or add work, and don't do a unit's work yourself: its agent, command or tool does.

Arguments: the first is the run id: the number of the run's tracker item, or a manual id. Pass `--pipeline` and `--profile` on to every `fab0-resolve` call. `--unit U` goes to `next` only, and limits the hand-outs to that unit.

## Rules

- Never merge, push, open or edit pull requests, or write to the tracker yourself. `apply` does all of it, within the project's merge policy.
- Never force-push, rebase, stash or discard anyone's changes. If a checkout a hand-out names has changes nobody recorded, stop and ask.
- Pass every brief verbatim. Don't add to it, shorten it or explain it.
- Run every `fab0-resolve` command from the home checkout, never from inside a unit's worktree.
- Keep the person informed as the run goes. Show the engine's `line`s exactly as they are: don't rewrite them, and don't work out durations or times yourself.
- If a result carries `warnings`, show them. If one says the home checkout isn't on the base branch, stop and ask the person: that checkout's wiring may not be the project's.
- Write return files outside every checkout, in `<home git common dir>/fab0/returns/<run>/`. `git -C <home path> rev-parse --path-format=absolute --git-common-dir` gives the common dir.

## Before the loop

1. `fab0-resolve validate`. If it fails, show the errors and stop. If it fails with an `error` and a `fix` list (the engine is older than the installed plugin), show the `error` and every `fix` line verbatim, and stop: don't try another engine, and fetch, apply and hand out nothing. Keep its `workspace`: repository names and their local paths.
2. For each repository with `present: true` and an `origin` remote, run `git -C <path> fetch --prune origin`. Say which repositories are missing; the run may not need them.

## The loop

Repeat:

1. **Apply.** `fab0-resolve apply <run>`. Show what it did as a short list, one line per entry of `applied`, in its order, saying what the entry holds (a merge, a pull request with its link, a sync, an action, a signal) without interpreting it. If `applied` is empty, say in one line that `apply` did nothing. If it fails, show the error and stop. If `kind` is `done`, or `waiting` (the run's pull request waits on a person), go to the end.
2. **Hand-outs.** `fab0-resolve next <run>`. Take its `handouts`, leaving out those with `executor: action`: `apply` performs actions. If none are left, go to the end.
   Then, before any checkout or agent starts, show each remaining hand-out's `line` (its start line), one per hand-out, all of the round's together. Show nothing for an action.
3. **Checkouts.** For each hand-out, in the repository each entry of `branches` names:
   - an entry without `worktree` that doesn't exist: `git -C <path> branch <branch> <from>`. Another hand-out may have created it already; that is fine.
   - the entry with `worktree`: if that path is already a checkout of `branch`, use it. Otherwise `git -C <path> worktree add <worktree> <branch>` when `exists` is true, or `git -C <path> worktree add -b <branch> <worktree> <from>` when it isn't.
   - with `sync: true`: `git -C <worktree> merge --no-edit <from>`. If it stops on conflicts (listed in `conflicts`), leave them: the brief tells the agent to resolve them, and the commit after `record` concludes the merge.
   - a gate hand-out (`gate: true`) names one `worktree` on `branch`: add it the same way if it isn't there.
4. **Execute**, every hand-out of this round:
   - **agent** (a unit or a gate): call the Agent tool with `subagent_type` = `agent`, `model` = `model` and `prompt` = `brief`. Send all of the round's Agent calls in one message so they run in parallel, in the foreground: never with `run_in_background`, and never one after the other to make them easier to follow. Claude Code then shows each agent's activity (its tool calls) while it works. Wait for every one. Take the JSON object from each agent's final message (inside a code fence, take the fence's content) and write it to `<returns>/<unit>.json`, or `<unit>-gate.json` for a gate.
   - **command**: run `command` with the unit's `worktree` (a gate's `worktree`) as the working directory, and keep its exit code. For a gate, also save its output to `<returns>/<unit>-gate.txt`.
   - **tool**: call the tool named in `tool` with `with` as its arguments. If this session doesn't have that tool, don't imitate it: report it and stop. Then write the return, valid against `return_schema`: `summary` is one line on what the call did, plus `data` if the schema asks for it, taken from the tool's result.
5. **Record**, each hand-out:
   - a unit: `fab0-resolve record <run> <unit> --return <file>`, or `--exit <code>` for a command;
   - a gate: `fab0-resolve record <run> <unit> --gate --return <file>`, or `--gate --exit <code> --output <file>` for a command.
   - Show the result's `line` (its end line: the outcome and how long it took) as soon as it is recorded, for every unit and gate, verified or not. When a result has no `line`, say that it was recorded.
   - If it is refused because the return doesn't match its schema, call the same agent once more with the brief followed by the `errors`, and record again. If it is refused again, or refused for any other reason (writes outside the unit's claims, a missing checkout), show the refusal and stop. The person decides how to undo writes; then the same `record` works.
   - `verified: false` is not a failure of yours: say so and go on. The next round hands the unit out again with the failures in its brief.
   - If the result lists `outside`, tell the person: files appeared beside the repositories while the unit ran.
6. **Commit** what `record` wrote, with the work, in the checkout it wrote to (each `worktree` of the unit, or the gate's):
   ```bash
   git -C <worktree> add -A && git -C <worktree> commit -m "<unit> attempt <attempt> (fab0 <fab0_version>): <first line of the summary>"
   ```
   For a gate: `"<unit> gate <attempt>: <decision, or exit code>"`. Don't push; `apply` pushes what it merges, opens a pull request for, or keeps on the ledger branch.
7. Go back to 1.

Stop early if a round hands out exactly what the round before handed out, with the same attempts, after they were recorded: something is wrong. Report it.

## At the end

1. `fab0-resolve status <run>`.
2. For each unit now `done` whose checkout lies under `<common dir>/fab0/worktrees/`, remove it with `git -C <path> worktree remove <worktree>`, if it is clean. Never remove a checkout the person made.
3. Report briefly:
   - what ran: each unit or gate, its executor, attempt and model, and whether it was verified or what the gate decided;
   - how long each unit took: the `took` of every unit in `status` that has one. Leave it out for a unit that has none;
   - what `apply` did over the run, with links to pull requests and items;
   - what the run waits for now, and on whom: units `waiting` (a human gate, questions, an escalation) and units `blocked` with their reasons. Leave out units that only wait their turn (`order`, `data`, `claim`, `limit`).

Then stop. Don't start other runs.
