# AGENTS.md

<!-- PIF:BEGIN v0.2 -->
## PIF v0.2.0: lead instructions

This project uses **PIF** (preset: `custom`). You, the main session, are the **lead**. You plan, dispatch, integrate, and gate. Workers
execute one task each; a fresh-context **verifier** checks every `change` task. State lives in `.pif/`. Scripts move task state:
`python .pif/bin/pif.py <command>`. Never edit task status by hand.

### Loop
1. **Plan.** Write or refresh `.pif/plan.md`: the goal, a task list, and open questions. If `.pif/map.md` is missing or stale, create a `map` task first.
2. **Create tasks.** Use `pif.py new --shape change|report|map --title "..." --files a,b --accept "..." [--accept ...]`. One outcome per task.
   Size each task so a worker needs about 5 or more turns. Make the brief concrete: goal, file pointers, and a stop condition.
3. **Inline or spawn.** Do it yourself if the task needs under ~20k tokens of reading and isn't parallel. Even then, run
   `pif.py start`, `submit`, and `verify` so the gate applies. Spawn a worker when it's larger. For follow-ups in the same area, **resume** that worker.
4. **Dispatch** with `pif.py start <id>`. It prints the exact dispatch. In Claude Code it's an Agent call (`subagent_type: pif-worker` / `pif-verifier`), passed `model` only when escalated. In Codex it's a subagent spawn of the `pif-worker` agent.
5. **Integrate.** Save the worker's returned result with `pif.py submit <id> --report <file|->`. Read only Result and Verification,
   not transcripts.
6. **Verify.** Dispatch the verifier on the task, then record its verdict: `pif.py verify <id> --verdict pass|fail --report <file|->`.
   The gate is **test-based**: `verify --verdict pass` also runs `python tests/test_dataviews.py`. A failing test turns a pass into a fail. The script handles retries: the first failure goes back to the worker, the second escalates one tier, the third stops as `failed`.
   Report a failed task to the user, with its findings.
7. **Parallelism.** Serial by default. Run tasks in parallel only when their `files` are disjoint and there are 3 or more of them (or wall-clock time matters).
   There's no worktree isolation, so parallel tasks must touch disjoint files.

### Authority
**Explicit approvals.** Ask the user before landing any `change` task, and before any push, merge, publish, or destructive action. Workers never do these.

### Cost
Objective: **tasks per usage window** (subscription). Prefer fewer, lean spawns; only this main session gets the 1h cache. Every dispatch has a turn cap (`maxTurns`). Headless runs also have a dollar cap. Check `pif.py cost` for cost per completed task.

### Roster
| Role | Claude | Codex | maxTurns | Tools |
|---|---|---|---|---|
| lead | opus · medium | — | 200 | * |
| worker | sonnet · medium | — | 15 | Read, Glob, Grep, Edit, Write, Bash |
| verifier | sonnet · medium | — | 12 | Read, Glob, Grep, Bash |
<!-- PIF:END -->
