# Multi-agent coding pipeline — starter

A long-running orchestrator that drives GitHub issues from "open" to "merged"
using a fleet of headless Claude Code agents, then keeps proposing new features
until you stop it.

This is a **skeleton to adapt**, not a finished product. Read `orchestrator.py`
and the prompts before pointing it at anything you care about.

## What's here
```
agents.yaml          # all config: project commands, knobs, per-role agent defs
orchestrator.py      # the daemon: clock, queue, 2-slot pool, state machine, verifier gate
start.sh             # preflight checks + launch
prompts/
  worker.md          # implements one issue, opens a PR, stops
  verifier.md        # gates the PR: pass / fail (with feedback) / escalate
  ideator.md         # after each merge, proposes next features
  heartbeat.md       # every 30 min, status + questions for you
  pm.md              # interactive: turns your idea into a GitHub issue (run by hand)
state/               # created at runtime: pipeline.db, ideas.md, questions.md, log
```

## Prerequisites
- `claude` (Claude Code CLI), `gh` (GitHub CLI, authenticated), and `git` on your PATH.
- Python 3.10+ and `pip install pyyaml`.
- A GitHub repo with some open issues. Label them `P0`–`P3` to set priority
  (default is `P2`).

## Run it
Drop these files at the **root of your git repo** (or copy `orchestrator.py`,
`agents.yaml`, and `prompts/` there), then:

```bash
chmod +x start.sh
./start.sh          # preflight + launch; Ctrl-C to stop
```

It loops forever: every 3 minutes it seeds the queue from open issues and fills any
free worker slots; every 30 minutes it writes a heartbeat. Stop it any time with
Ctrl-C — on restart it recovers (resets anything mid-flight back to the queue and
prunes leftover worktrees).

## Feeding it work
Run the Project Manager interactively whenever you have an idea — it interviews you,
then files a well-formed issue the orchestrator will pick up automatically:

```bash
claude --append-system-prompt "$(cat prompts/pm.md)"
```

## Where to look while it runs
- `state/orchestrator.log` — everything the daemon does, with per-agent cost.
- `state/questions.md` — heartbeats + anything escalated to you. **Check this.**
- `state/ideas.md` — the ideator's running list of feature proposals.

## The lifecycle (per issue)
```
QUEUED -> IN_PROGRESS -> PR_OPEN -> [test gate] -> VERIFYING
   pass  -> MERGED -> worktree removed -> DONE   (frees slot, triggers Ideator)
   fail  -> NEEDS_REPAIR -> back to the same worker, same branch
   stuck -> ESCALATED (written to questions.md) after max_repair_rounds
```
A task occupies **one of the 2 slots for its entire lifecycle**, including
verification and repair — so the worktree (the "agent") is deleted only after the
verifier passes and the PR merges. That's the "delete only after verified" rule, and
it caps concurrent worktrees/coding agents at `max_workers`.

## Design choices worth knowing
- **Each task gets its own git worktree + branch**, so 2 agents never collide on
  files. Git-mutating ops (worktree add/remove, merge) are serialized with a lock;
  the actual coding and tests run fully in parallel.
- **A cheap deterministic gate runs before the verifier.** If `npm test`/build fails,
  the task bounces straight back to the worker without spending verifier tokens. (The
  verifier re-checks anyway as defense in depth — trim that from `verifier.md` if you
  want to save tokens and trust the pre-gate.)
- **The PM and the human-facing half of the heartbeat are deliberately not headless.**
  Headless sessions can't prompt you interactively, so the heartbeat *writes* its
  questions to `questions.md` and the PM runs as a real interactive session.

## Cost — read before an overnight run
A continuous 2-agent loop burns tokens fast. For a fleet this size, prefer billing
against an **API key** (`export ANTHROPIC_API_KEY=...`) rather than a subscription
seat. Note also that Agent SDK / `claude -p` usage on subscription plans is metered
separately from interactive use as of mid-2026 — check your plan's current terms.

Guardrails already wired in: `--max-turns` and a per-task `--max-budget-usd` on every
agent. The budget cap is tallied per turn, so a run can slightly overshoot before it
trips — set it with headroom, and keep an eye on the per-agent costs in the log.

## Safety
- **Protect your base branch** (require passing checks / no direct pushes) so nothing
  merges even if a bug in this orchestrator tries to. The verifier is your quality
  wall — keep it adversarial.
- The configs use scoped `allowed_tools` and `acceptEdits`. **Don't** swap in
  `--dangerously-skip-permissions`; if you must run wide-open, do it in a container.
- Tune `agents.yaml`: the gate commands, the models (drop workers to Sonnet to save
  cost), tool scopes, poll/heartbeat intervals, and `max_repair_rounds`.

## Known simplifications (it's a skeleton)
- If the daemon is killed mid-task, the task returns to the queue and a *new* branch/PR
  is created on the next attempt; the orphaned PR (if any) is left for you to close.
- Priority is a simple label sort; there's no semantic prioritization.
- No retry/backoff around transient `gh`/`git` failures beyond escalation.
