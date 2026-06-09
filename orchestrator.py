#!/usr/bin/env python3
"""
orchestrator.py — long-running daemon that drives a multi-agent coding pipeline.

Roles (configured in agents.yaml, prompts in prompts/):
  - Working agents : implement one GitHub issue each, in an isolated git worktree.
  - Verifier       : gates each PR before it can merge.
  - Ideator        : after each merge, proposes follow-up features (writes ideas file).
  - Heartbeat      : every 30 min, writes a status report + questions for the human.
  - Project Manager: interactive, run by hand (NOT by this daemon) — see prompts/pm.md.

Each "agent" is a headless `claude -p` session spawned from its config. This file
owns the clock, the task queue, the N-slot worker pool, the per-task state
machine, and the verifier gate.

A task holds one of the N worker slots for its WHOLE lifecycle (code -> gate ->
verify -> repair -> merge). The worktree (the "agent") is torn down only after the
verifier passes and the PR merges — exactly the "delete only after verified" rule.

Prereqs on PATH: claude, gh (authenticated), git. Place this folder at the root
of your git repo and run from there.  See README.md.
    pip install pyyaml
    python orchestrator.py
"""

import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "agents.yaml").read_text())
PROJECT = CFG["project"]
ORCH = CFG["orchestrator"]
AGENTS = CFG["agents"]

STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)
DB_PATH = STATE_DIR / "pipeline.db"

# Wall-clock ceilings per role (seconds); a hung session can't block a slot forever.
DEFAULT_TIMEOUTS = {"worker": 2400, "verifier": 1200, "ideator": 900, "heartbeat": 600}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(threadName)-10s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(STATE_DIR / "orchestrator.log")],
)
log = logging.getLogger("orchestrator")

# Serialize git ops that mutate shared repo state (worktree add/remove, merges).
# Tests inside a worktree run OUTSIDE this lock, so 2 agents still build in parallel.
git_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# State store (thread-safe SQLite — opens a connection per call)
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    issue        INTEGER PRIMARY KEY,
                    title        TEXT,
                    body         TEXT,
                    priority     INTEGER DEFAULT 2,   -- P0=0 .. P3=3 (lower = sooner)
                    state        TEXT DEFAULT 'QUEUED',
                    pr           INTEGER,
                    branch       TEXT,
                    worktree     TEXT,
                    repair_round INTEGER DEFAULT 0,
                    feedback     TEXT,
                    updated      REAL
                );
            """)

    def _connect(self):
        # Returns a WAL-mode connection with row factory and busy timeout set.
        c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA busy_timeout=30000;")
        return c

    def upsert_issue(self, issue, title, body, priority):
        """Insert new issues as QUEUED; refresh metadata without touching live state."""
        with self._lock, self._connect() as c:
            row = c.execute("SELECT 1 FROM tasks WHERE issue=?", (issue,)).fetchone()
            if row is None:
                c.execute("INSERT INTO tasks(issue,title,body,priority,state,updated) "
                          "VALUES(?,?,?,?,'QUEUED',?)",
                          (issue, title, body, priority, time.time()))
            else:
                c.execute("UPDATE tasks SET title=?, body=?, priority=? WHERE issue=?",
                          (title, body, priority, issue))

    def set(self, issue, **fields):
        # Update arbitrary columns on a task row; always stamps updated timestamp.
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._connect() as c:
            c.execute(f"UPDATE tasks SET {cols} WHERE issue=?", (*fields.values(), issue))

    def get(self, issue):
        # Fetch a single task row as a dict, or None if not found.
        with self._connect() as c:
            r = c.execute("SELECT * FROM tasks WHERE issue=?", (issue,)).fetchone()
            return dict(r) if r else None

    def queued(self, limit):
        # Return up to `limit` QUEUED tasks ordered by priority then issue number.
        with self._connect() as c:
            rows = c.execute("SELECT * FROM tasks WHERE state='QUEUED' "
                             "ORDER BY priority ASC, issue ASC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def by_state(self, *states):
        # Return all tasks whose state matches any of the given values.
        ph = ",".join("?" * len(states))
        with self._connect() as c:
            rows = c.execute(f"SELECT * FROM tasks WHERE state IN ({ph}) ORDER BY issue",
                             states).fetchall()
            return [dict(r) for r in rows]

    def reset_stale(self):
        """On startup, return anything that was mid-flight to the queue."""
        stale = ("IN_PROGRESS", "PR_OPEN", "VERIFYING", "NEEDS_REPAIR")
        with self._lock, self._connect() as c:
            c.execute(f"UPDATE tasks SET state='QUEUED', worktree=NULL, repair_round=0 "
                      f"WHERE state IN ({','.join('?'*len(stale))})", stale)

    def snapshot(self):
        # Return a dict mapping each state to its task count.
        with self._connect() as c:
            rows = c.execute("SELECT state, COUNT(*) n FROM tasks GROUP BY state").fetchall()
            return {r["state"]: r["n"] for r in rows}


db = Store(DB_PATH)


# --------------------------------------------------------------------------- #
# Spawning headless Claude agents
# --------------------------------------------------------------------------- #
def _render(prompt_file, variables):
    # Load a prompt file and substitute all {{key}} placeholders with their values.
    text = (ROOT / prompt_file).read_text()
    for k, v in variables.items():
        text = text.replace("{{%s}}" % k, "" if v is None else str(v))
    return text


def extract_json(text):
    """Pull the agent's final JSON object out of its message, defensively.

    Agents are told to emit one bare JSON line, but models sometimes wrap it in a
    code fence or add stray prose — so we try several strategies, last-match-wins.
    """
    if not text:
        return None
    stripped = text.strip()
    candidates = [stripped]
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if lines:
        candidates.append(lines[-1].strip())              # the final line
    fence = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", stripped, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))                  # fenced block
    blocks = re.findall(r"[\[{].*[\]}]", stripped, re.DOTALL)
    if blocks:
        candidates.append(blocks[-1])                      # greedy last object
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def call_agent(role, variables, cwd="."):
    """Run a role's headless session to completion. Returns (parsed_json, envelope)."""
    spec = AGENTS[role]
    prompt = _render(spec["prompt_file"], variables)
    cmd = [
        "claude", "-p", prompt,
        "--model", spec["model"],
        "--output-format", "json",
        "--max-turns", str(spec.get("max_turns", 30)),
        "--allowedTools", ",".join(spec.get("allowed_tools", [])),
    ]
    if spec.get("permission_mode"):
        cmd += ["--permission-mode", spec["permission_mode"]]
    if spec.get("max_budget_usd"):
        cmd += ["--max-budget-usd", str(spec["max_budget_usd"])]

    timeout = spec.get("timeout_seconds", DEFAULT_TIMEOUTS.get(role, 1200))
    log.info("spawn %-9s (model=%s, cwd=%s)", role, spec["model"], cwd)
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("%s timed out after %ss", role, timeout)
        return None, {"is_error": True, "result": "", "error": "timeout"}
    except OSError as e:
        # e.g. the binary (claude/gh/git) isn't on this process's PATH, or cwd vanished.
        log.error("%s could not launch %r: %s", role, cmd[0], e)
        return None, {"is_error": True, "result": "", "error": f"exec-failed: {e}"}

    # `--output-format json` prints one JSON envelope to stdout.
    out = proc.stdout.strip()
    envelope = {}
    try:
        envelope = json.loads(out)
    except Exception:
        tail = [ln for ln in out.splitlines() if ln.strip()]
        if tail:
            try:
                envelope = json.loads(tail[-1])
            except Exception:
                envelope = {"result": out}
    cost = envelope.get("total_cost_usd")
    log.info("%-9s done (cost=%s, session=%s, exit=%s)",
             role, f"${cost:.4f}" if cost else "?", envelope.get("session_id"), proc.returncode)
    if proc.returncode != 0 and not envelope.get("result"):
        log.error("%s stderr tail: %s", role, proc.stderr[-400:])
    return extract_json(envelope.get("result", "")), envelope


# --------------------------------------------------------------------------- #
# Git / GitHub helpers
# --------------------------------------------------------------------------- #
def _run(cmd, **kw):
    # Thin subprocess.run wrapper with captured output and text mode on by default.
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def make_worktree(issue):
    """Create a fresh branch + worktree off the latest base branch."""
    base = PROJECT["base_branch"]
    branch = f"agent/issue-{issue}-{secrets.token_hex(2)}"
    wt = (ROOT / PROJECT["worktree_root"] / f"issue-{issue}").resolve()
    with git_lock:
        _run(["git", "fetch", "origin", base])
        if wt.exists():
            _run(["git", "worktree", "remove", "--force", str(wt)])
        wt.parent.mkdir(parents=True, exist_ok=True)
        r = _run(["git", "worktree", "add", "-b", branch, str(wt), f"origin/{base}"])
        if r.returncode != 0:
            raise RuntimeError(f"worktree add failed: {r.stderr.strip()}")
    log.info("issue #%s -> branch %s", issue, branch)
    return str(wt), branch


def remove_worktree(wt, branch=None):
    # Force-remove the worktree directory and optionally delete the local branch.
    if not wt:
        return
    with git_lock:
        _run(["git", "worktree", "remove", "--force", wt])
        if branch:
            _run(["git", "branch", "-D", branch])     # delete the now-unused local branch


def find_pr_for_branch(branch):
    # Query gh CLI for the open PR number associated with the given branch.
    r = _run(["gh", "pr", "list", "--head", branch, "--json", "number",
              "--jq", ".[0].number"])
    out = r.stdout.strip()
    return int(out) if out.isdigit() else None


def merge_pr(pr):
    # Merge a PR via gh CLI using the merge method from config (default: squash).
    # NOTE: deliberately NOT passing --delete-branch — the branch is still checked
    # out in the worktree at this point, so gh's local-branch deletion would fail
    # and make a *successful* remote merge look failed. Branch cleanup is done
    # afterwards by remove_worktree (local) + delete_remote_branch (remote).
    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}[
        PROJECT.get("merge_method", "squash")]
    with git_lock:
        r = _run(["gh", "pr", "merge", str(pr), flag])
    if r.returncode != 0:
        log.error("merge of PR #%s failed: %s", pr, r.stderr.strip())
        return False
    return True


def delete_remote_branch(branch):
    # Best-effort delete of the remote branch after a merge; failure is non-fatal.
    if not branch:
        return
    r = _run(["git", "push", "origin", "--delete", branch])
    if r.returncode != 0:
        log.warning("could not delete remote branch %s: %s", branch, r.stderr.strip()[-200:])


def push_branch(branch, wt):
    """Push the branch to origin from its worktree. Raise on failure so callers escalate."""
    r = _run(["git", "push", "-u", "origin", branch], cwd=wt)
    if r.returncode != 0:
        raise RuntimeError(f"git push of {branch} failed: {r.stderr.strip()[-300:]}")


def has_new_commits(wt):
    # True if the worktree HEAD is ahead of the base branch (the agent actually committed).
    base = PROJECT["base_branch"]
    r = _run(["git", "rev-list", "--count", f"origin/{base}..HEAD"], cwd=wt)
    out = r.stdout.strip()
    return r.returncode == 0 and out.isdigit() and int(out) > 0


def open_pr(issue, title, branch, wt, summary):
    """Create a PR for the branch (orchestrator-owned) and return its number, or None."""
    base = PROJECT["base_branch"]
    body = f"Closes #{issue}\n\n{summary or 'Automated PR opened by the orchestrator.'}"
    r = _run(["gh", "pr", "create", "--base", base, "--head", branch,
              "--title", title, "--body", body], cwd=wt)
    if r.returncode != 0:
        log.error("gh pr create failed for issue #%s: %s", issue, r.stderr.strip()[-300:])
    return find_pr_for_branch(branch)


# --------------------------------------------------------------------------- #
# Deterministic gate — cheap pre-check so the verifier doesn't burn tokens on
# mechanically-broken code. Runs the project's own commands inside the worktree.
# --------------------------------------------------------------------------- #
def run_gates(wt):
    for key in ("test_cmd", "build_cmd", "lint_cmd"):
        cmd = PROJECT.get(key)
        if not cmd:
            continue
        r = _run(cmd, cwd=wt, shell=True)             # cmd is trusted config
        if r.returncode != 0:
            tail = (r.stdout + r.stderr)[-1500:]
            return False, f"`{cmd}` failed:\n{tail}"
    return True, ""


# --------------------------------------------------------------------------- #
# Human-facing outputs (the heartbeat report + escalations land here)
# --------------------------------------------------------------------------- #
def _append(path_key, header, body):
    # Append a timestamped markdown section to the file named by path_key in config.
    p = ROOT / ORCH[path_key]
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} — {header}\n\n{body}\n")


def append_questions(header, body):
    # Write a new entry to questions.md for the human to review.
    _append("questions_file", header, body)


def append_ideas(merged_title, ideas):
    # Format ideator output as a bullet list and append it to ideas.md.
    rows = "".join(
        f"- **[{i.get('priority', '?')}] {i.get('title', '(untitled)')}** — "
        f"{i.get('rationale', '')} _{i.get('scope', '')}_\n" for i in ideas)
    _append("ideas_file", f"after merging: {merged_title}", rows)


# --------------------------------------------------------------------------- #
# The task lifecycle (runs in a worker-pool thread; holds ONE of N slots)
# --------------------------------------------------------------------------- #
TRANSIENT_CAP = 3                  # per-issue infra-blip requeues before we escalate
_transient_lock = threading.Lock()
_transient_fails = {}              # issue -> consecutive infra blips; reset on any commit


def _infra_blip(env):
    """True only when *nothing actually ran* — safe to retry. Distinguishes a real
    availability/exec blip from a session that DID run (spent tokens) but produced no
    commits (budget cap, max turns, ignored contract) — the latter is a failed attempt,
    not a blip, and must go through repair/escalate instead of looping forever."""
    err = env.get("error")
    if err == "exec-failed":       # the binary couldn't even launch
        return True
    if err == "timeout":           # it ran the full wall-clock — not a quick retry
        return False
    return not env.get("total_cost_usd")   # cost=? with no error -> availability/auth blip


def _bump_transient(issue):
    with _transient_lock:
        _transient_fails[issue] = _transient_fails.get(issue, 0) + 1
        return _transient_fails[issue]


def _reset_transient(issue):
    with _transient_lock:
        _transient_fails.pop(issue, None)


def work_task(issue):
    t = db.get(issue)
    try:
        wt, branch = make_worktree(issue)
        db.set(issue, worktree=wt, branch=branch, state="IN_PROGRESS")
        push_branch(branch, wt)         # branch exists on origin immediately; auth fails fast
    except Exception as e:
        log.exception("issue #%s: could not start", issue)
        db.set(issue, state="ESCALATED")
        append_questions(f"Issue #{issue} could not start",
                         f"Worktree creation or initial push failed: {e}")
        return

    max_rounds = ORCH.get("max_repair_rounds", 3)
    feedback = None
    pr = None
    for rnd in range(max_rounds + 1):
        db.set(issue, repair_round=rnd, state="IN_PROGRESS")
        # The agent only writes code + commits; the orchestrator owns push / PR / merge.
        wout, env = call_agent("worker", {
            "issue_number": issue, "issue_title": t["title"], "issue_body": t["body"],
            "branch_name": branch, "repair_round": rnd,
            "verifier_feedback": feedback or "(none)",
        }, cwd=wt)

        # The real signal is whether the agent COMMITTED — not its (advisory) JSON.
        # This salvages partial work from a run that hit the budget/turn cap mid-task.
        if not has_new_commits(wt):
            if (wout or {}).get("status") == "blocked":
                reason = (wout or {}).get("reason", "worker reported it was blocked")
                log.warning("issue #%s: worker blocked (%s)", issue, reason)
                db.set(issue, state="ESCALATED")
                append_questions(f"Issue #{issue} blocked", reason)
                remove_worktree(wt, branch)
                delete_remote_branch(branch)
                return

            if _infra_blip(env):
                # Nothing ran (availability/auth/exec). Requeue, but cap the retries so a
                # sustained outage can't loop forever burning a slot.
                n = _bump_transient(issue)
                if n > TRANSIENT_CAP:
                    log.error("issue #%s: %d infra blips -> escalate", issue, n)
                    db.set(issue, state="ESCALATED")
                    append_questions(f"Issue #{issue} repeated infra failures",
                                     f"Worker could not run {n} times ({env.get('error') or 'no inference'}) "
                                     f"— check model availability / auth.")
                    _reset_transient(issue)
                    remove_worktree(wt, branch)
                    delete_remote_branch(branch)
                    return
                log.warning("issue #%s: infra blip (%s) -> requeue (%d/%d)",
                            issue, env.get("error") or "no inference", n, TRANSIENT_CAP)
                remove_worktree(wt, branch)
                delete_remote_branch(branch)
                db.set(issue, state="QUEUED", worktree=None, pr=None, repair_round=0)
                return

            # It ran (spent tokens) but committed nothing — likely hit the budget/turn cap
            # or ignored the contract. Treat as a failed attempt: repair (same worktree),
            # escalate after max rounds. NOT a transient requeue.
            feedback = ("Your previous attempt ended without committing anything (most likely it "
                        "hit the turn or budget limit). Work in small steps and COMMIT frequently "
                        "so progress is saved across attempts. Do not push or open a PR — the "
                        "orchestrator does that.")
            log.info("issue #%s: no commits despite inference -> repair", issue)
            db.set(issue, state="NEEDS_REPAIR", feedback=feedback)
            continue

        _reset_transient(issue)     # made progress — clear the infra-blip counter

        try:
            push_branch(branch, wt)
        except Exception as e:
            log.error("issue #%s: push failed: %s", issue, e)
            db.set(issue, state="ESCALATED")
            append_questions(f"Issue #{issue} push failed", str(e))
            remove_worktree(wt, branch)
            delete_remote_branch(branch)
            return

        pr = pr or find_pr_for_branch(branch) or open_pr(
            issue, t["title"], branch, wt, (wout or {}).get("summary"))
        if not pr:
            log.warning("issue #%s: could not open a PR", issue)
            db.set(issue, state="ESCALATED")
            append_questions(f"Issue #{issue} no PR",
                             "Commits were pushed but a PR could not be created — see the log.")
            remove_worktree(wt, branch)
            delete_remote_branch(branch)
            return
        db.set(issue, pr=pr, state="PR_OPEN")
        log.info("issue #%s: PR #%s open", issue, pr)

        # 1) cheap deterministic gate
        ok, gate_out = run_gates(wt)
        if not ok:
            feedback = gate_out
            log.info("issue #%s: gates failed -> repair", issue)
            db.set(issue, state="NEEDS_REPAIR", feedback=feedback)
            continue

        # 2) semantic review by the verifier
        db.set(issue, state="VERIFYING")
        vout, _ = call_agent("verifier", {
            "issue_number": issue, "issue_title": t["title"], "issue_body": t["body"],
            "pr_number": pr, "branch_name": branch, "repair_round": rnd,
        }, cwd=wt)
        verdict = (vout or {}).get("verdict")

        if verdict == "pass":
            if merge_pr(pr):
                db.set(issue, state="MERGED")
                remove_worktree(wt, branch)              # <- agent "deleted" only now
                delete_remote_branch(branch)             # remote cleanup after merge
                db.set(issue, state="DONE")
                log.info("issue #%s: merged + cleaned up. DONE", issue)
                bg_pool.submit(run_ideator, issue, t["title"])   # post-merge trigger
            else:
                db.set(issue, state="ESCALATED")
                append_questions(f"Issue #{issue} merge failed",
                                 f"Verifier passed PR #{pr} but merge failed — check "
                                 f"for conflicts with the base branch or branch protection.")
                remove_worktree(wt, branch)
            return

        if verdict == "escalate":
            db.set(issue, state="ESCALATED")
            append_questions(f"Issue #{issue} needs you",
                             (vout or {}).get("summary", "Verifier could not adjudicate."))
            remove_worktree(wt, branch)
            return

        # verdict == "fail" (or anything unexpected) -> send back to the worker
        feedback = "\n".join((vout or {}).get("feedback", [])) or \
                   (vout or {}).get("summary", "Verifier rejected the PR.")
        log.info("issue #%s: verifier rejected (round %s)", issue, rnd)
        db.set(issue, state="NEEDS_REPAIR", feedback=feedback)

    # ran out of repair rounds
    log.warning("issue #%s: still failing after %s rounds -> escalate", issue, max_rounds)
    db.set(issue, state="ESCALATED")
    append_questions(f"Issue #{issue} stuck",
                     f"Still failing after {max_rounds} repair rounds.\n\nLast feedback:\n{feedback}")
    remove_worktree(wt, branch)


# --------------------------------------------------------------------------- #
# Ideator + heartbeat (background pool; do NOT consume the N worker slots)
# --------------------------------------------------------------------------- #
def run_ideator(issue, title):
    # Trigger the ideator agent after a merge and log any proposed feature ideas.
    out, _ = call_agent("ideator", {"merged_feature": title, "issue_number": issue})
    ideas = (out or {}).get("ideas", [])
    if ideas:
        append_ideas(title, ideas)
        log.info("ideator: logged %d ideas after #%s", len(ideas), issue)


def run_heartbeat():
    # Assemble current pipeline state and run the heartbeat agent; append its report to questions.md.
    snap = db.snapshot()
    inflight_rows = db.by_state("IN_PROGRESS", "PR_OPEN", "VERIFYING", "NEEDS_REPAIR")
    escalated = db.by_state("ESCALATED")
    state_txt = (
        f"State counts: {snap}\n"
        f"In flight: " + (", ".join(f"#{r['issue']}({r['state']})" for r in inflight_rows) or "none") +
        f"\nEscalated / needs you: " + (", ".join(f"#{r['issue']}" for r in escalated) or "none")
    )
    out, _ = call_agent("heartbeat", {"pipeline_state": state_txt})
    if out and out.get("report"):
        append_questions("Heartbeat", out["report"])
        log.info("heartbeat written (needs_decision=%s)", out.get("needs_decision"))


# --------------------------------------------------------------------------- #
# Seeding the queue from GitHub
# --------------------------------------------------------------------------- #
PRIORITY_LABELS = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def issue_priority(labels):
    # Parse a P0–P3 label from a GitHub issue's label list; defaults to P2.
    for lab in labels:
        if lab.get("name", "").upper() in PRIORITY_LABELS:
            return PRIORITY_LABELS[lab["name"].upper()]
    return 2  # default P2


def seed_queue():
    # Fetch open GitHub issues and upsert them into the task queue (new issues start as QUEUED).
    r = _run(["gh", "issue", "list", "--state", "open", "--limit", "100",
              "--json", "number,title,body,labels"])
    if r.returncode != 0:
        log.error("gh issue list failed: %s", r.stderr.strip())
        return
    for it in json.loads(r.stdout or "[]"):
        db.upsert_issue(it["number"], it["title"], it.get("body") or "",
                        issue_priority(it.get("labels", [])))


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
worker_pool = ThreadPoolExecutor(max_workers=ORCH["max_workers"], thread_name_prefix="worker")
bg_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bg")
inflight = {}   # issue -> Future. Lives in the main thread; the slot-count authority.


def reconcile():
    # Prune finished worker futures, then fill any free slots with the next queued tasks.
    for issue, fut in list(inflight.items()):
        if fut.done():
            if fut.exception():
                log.error("issue #%s task crashed: %r", issue, fut.exception())
            inflight.pop(issue, None)

    free = ORCH["max_workers"] - len(inflight)
    if free <= 0:
        return
    for task in db.queued(limit=free):
        issue = task["issue"]
        if issue in inflight:
            continue
        db.set(issue, state="IN_PROGRESS")             # claim before submit
        inflight[issue] = worker_pool.submit(work_task, issue)
        log.info("assigned issue #%s  (%d/%d slots in use)",
                 issue, len(inflight), ORCH["max_workers"])


REQUIRED_BINARIES = ("claude", "gh", "git")


def preflight():
    """Fail fast (with an actionable message) if a required CLI isn't on this process's PATH.

    Mirrors start.sh's bash preflight, but inside Python so it also fires when the daemon
    is launched from an IDE Run/Debug button (which on macOS gets launchd's reduced PATH,
    typically missing ~/.local/bin where `claude` lives).
    """
    missing = [b for b in REQUIRED_BINARIES if shutil.which(b) is None]
    if missing:
        log.error("Required executable(s) not found on PATH: %s", ", ".join(missing))
        log.error("PATH this process sees:\n  %s",
                  "\n  ".join(os.environ.get("PATH", "").split(os.pathsep)))
        log.error("If launched from an IDE Run/Debug button, it has a reduced PATH. "
                  "Run from a terminal, set console=integratedTerminal in launch.json, "
                  "or add the missing binary's dir (e.g. ~/.local/bin) to PATH.")
        sys.exit(1)


def main():
    # Entry point: recover state from a prior run, then poll forever seeding and reconciling the queue.
    preflight()
    log.info("startup: pruning stale worktrees + recovering state")
    _run(["git", "worktree", "prune"])
    db.reset_stale()
    seed_queue()

    last_beat = time.time()
    log.info("loop start (poll=%ss, heartbeat=%ss, max_workers=%s). Ctrl-C to stop.",
             ORCH["poll_seconds"], ORCH["heartbeat_seconds"], ORCH["max_workers"])
    try:
        while True:
            seed_queue()                 # pick up issues the PM created since last poll
            reconcile()
            if time.time() - last_beat >= ORCH["heartbeat_seconds"]:
                bg_pool.submit(run_heartbeat)
                last_beat = time.time()
            time.sleep(ORCH["poll_seconds"])
    except KeyboardInterrupt:
        log.info("stopping: no new tasks assigned. In-flight sessions are killed on exit.")
        worker_pool.shutdown(wait=False, cancel_futures=True)
        bg_pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
