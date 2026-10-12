#!/usr/bin/env python3
"""PIF v0.2 mechanics: tasks, state transitions, dispatch, gates, escalation, usage, and the headless runner.

Agents own judgment; this script owns every state change (docs/design/protocol-v0.2.md §4). Stdlib only.
Lives at <project>/.pif/bin/pif.py and reads <project>/.pif/decisions.json.

  pif.py new --shape change --title "..." --files a.py,b.py --accept "..." [--accept "..."] [--depends T-0001]
  pif.py list | show <id> | cost
  pif.py start <id> [--exec] [--headless]        # print (or run headless) the worker dispatch
  pif.py submit <id> --report FILE|-              # save the worker result -> verify
  pif.py verify <id> --verdict pass|fail --report FILE|- [--no-gate]
  pif.py block <id> --reason "..."
  pif.py log-usage <id> --role worker --model m --usd 0.12 --turns 7
  pif.py run-all [--exec] [--max-tasks N]         # headless pipeline: worker -> verifier -> verify, per todo task
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PIF = Path(__file__).resolve().parent.parent
ROOT = PIF.parent
TASKS = PIF / "tasks"
LOG = PIF / "log.jsonl"
STATUSES = ("todo", "active", "verify", "done", "failed", "blocked")
EFFORT_LADDER = ["low", "medium", "high", "xhigh"]
MODEL_LADDER = {"claude": ["haiku", "sonnet", "opus"], "codex": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"]}
SECTIONS = ("Brief", "Result", "Verification")
# Always denied to headless agents (deny rules win over allow rules in Claude Code). Projects add more in commands.deny.
DENY_ALWAYS = ["Bash(git push*)", "Bash(git commit*)", "Bash(git reset*)", "Bash(git checkout*)", "Bash(git rebase*)",
               "Bash(git merge*)", "Bash(git branch -D*)", "Bash(rm -rf*)", "Bash(Remove-Item*)", "Bash(del *)"]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def decisions() -> dict:
    return json.loads((PIF / "decisions.json").read_text(encoding="utf-8"))


def log(event: str, task: str | None = None, **data) -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now(), "event": event, "task": task, **data}) + "\n")


# ---- task files: frontmatter values are JSON (or bare strings); body has ## Brief / ## Result / ## Verification ----
def read_task(tid: str) -> tuple[dict, dict]:
    path = TASKS / f"{tid}.md"
    if not path.is_file():
        raise SystemExit(f"no such task: {tid}")
    text = path.read_text(encoding="utf-8")
    _, head, body = text.split("---\n", 2)
    meta = {}
    for line in head.splitlines():
        key, _, raw = line.partition(": ")
        try:
            meta[key] = json.loads(raw)
        except json.JSONDecodeError:
            meta[key] = raw
    sections = {name: "" for name in SECTIONS}
    for m in re.finditer(r"^## (\w+)\n(.*?)(?=^## \w+\n|\Z)", body, re.M | re.S):
        if m.group(1) in sections:
            sections[m.group(1)] = m.group(2).strip()
    return meta, sections


def write_task(meta: dict, sections: dict) -> None:
    head = "\n".join(f"{k}: {v if isinstance(v, str) and k in ('id', 'shape', 'status', 'title') else json.dumps(v)}"
                     for k, v in meta.items())
    body = "\n\n".join(f"## {name}\n{sections.get(name, '').strip()}" for name in SECTIONS)
    (TASKS / f"{meta['id']}.md").write_text(f"---\n{head}\n---\n{body}\n", encoding="utf-8", newline="\n")


def all_tasks() -> list[dict]:
    return [read_task(p.stem)[0] for p in sorted(TASKS.glob("T-*.md"))]


def next_id() -> str:
    nums = [int(p.stem[2:]) for p in TASKS.glob("T-*.md") if p.stem[2:].isdigit()]
    return f"T-{max(nums, default=0) + 1:04d}"


def set_status(meta: dict, sections: dict, status: str, **data) -> None:
    assert status in STATUSES
    old = meta["status"]
    meta["status"] = status
    write_task(meta, sections)
    log("status", meta["id"], frm=old, to=status, **data)


def read_report(arg: str) -> str:
    return sys.stdin.read() if arg == "-" else Path(arg).read_text(encoding="utf-8")


# ---- commands ----
def cmd_new(a) -> None:
    d = decisions()
    role = d["roster"]["worker"]
    shape_override = (d["roster"].get("shapes") or {}).get(a.shape, {})
    tier = {h: shape_override.get(h, role.get(h)) for h in d["options"]["adapters"] if shape_override.get(h, role.get(h))}
    meta = {
        "id": next_id(), "title": a.title, "shape": a.shape, "status": "todo", "tier": tier,
        "files": [f for f in a.files.split(",") if f], "accept": a.accept,
        "budget": {"turns": a.turns or shape_override.get("max_turns", role["max_turns"]), "usd": a.usd or d["budgets"]["task_usd"]},
        "attempts": 0, "depends_on": a.depends or [], "created": now(),
    }
    brief = a.brief or f"Goal: {a.title}\nContext: see `files` and .pif/map.md\nStop when: every accept criterion is met, or a blocker is found."
    if d["commands"]["test"]:
        brief += "\nTests: " + " && ".join(d["commands"]["test"])
    write_task(meta, {"Brief": brief})
    log("new", meta["id"], shape=a.shape)
    print(meta["id"])


def cmd_list(_a) -> None:
    for m in all_tasks():
        tier = m["tier"].get("claude") or next(iter(m["tier"].values()), {})
        print(f"{m['id']}  {m['status']:<8} {m['shape']:<7} {tier.get('model', '?')}/{tier.get('effort', '?'):<6} "
              f"attempts={m['attempts']}  {m['title']}")


def cmd_show(a) -> None:
    print((TASKS / f"{a.id}.md").read_text(encoding="utf-8"))


def dispatch_spec(meta: dict, role: str, headless: bool) -> dict:
    """The exact dispatch for a role on a task, given the decisions' unit and adapters."""
    d = decisions()
    prompt = f"Task {meta['id']}. Brief: .pif/tasks/{meta['id']}.md" + (" Verify it." if role == "verifier" else "")
    harness = "claude" if "claude" in d["options"]["adapters"] else "codex"
    tier = meta["tier"].get(harness) if role == "worker" else d["roster"]["verifier"].get(harness)
    turns = meta["budget"]["turns"] if role == "worker" else d["roster"]["verifier"]["max_turns"]
    cap = round(meta["budget"]["usd"] * d["budgets"]["headless_cap_fraction"], 2)
    headless = headless or d["options"]["unit"] == "headless"
    if harness == "codex":
        cmd = ["codex", "exec", "-m", tier["model"], "-c", f'model_reasoning_effort="{tier["effort"]}"', prompt]
        return {"mode": "headless", "harness": "codex", "cmd": cmd, "turns": turns}
    if headless:
        writes = role == "worker" and meta["shape"] == "change"
        tools = [t for t in d["roster"][role]["tools"] if writes or t not in ("Edit", "Write")]
        deny = DENY_ALWAYS + d["commands"].get("deny", []) + ([] if writes else ["Edit", "Write"])
        claude_bin = shlex.split(os.environ.get("PIF_CLAUDE_BIN", "claude"), posix=os.name != "nt")
        claude_bin = [part.strip('"') for part in claude_bin]
        cmd = [*claude_bin, "-p", prompt, "--agent", f"pif-{role}", "--model", tier["model"], "--effort", tier["effort"],
               "--max-budget-usd", str(cap), "--output-format", "json",
               "--allowedTools", ",".join(tools), "--disallowedTools", ",".join(deny)]
        if writes:
            cmd += ["--permission-mode", "acceptEdits"]
        return {"mode": "headless", "harness": "claude", "cmd": cmd, "turns": turns, "cap_usd": cap}
    base = d["roster"][role]["claude"]
    call = {"subagent_type": f"pif-{role}", "prompt": prompt}
    if tier["model"] != base["model"]:
        call["model"] = tier["model"]  # escalated; in-process can override model only (effort is fixed per agent)
    return {"mode": "in-process", "harness": "claude", "agent_call": call, "turns": turns}


INFRA_PATTERNS = re.compile(r"failed to authenticate|oauth|not logged in|api key|rate limit|overloaded|"
                            r"agent .* not found|unknown option|command not found|credit balance", re.I)


class InfraError(RuntimeError):
    """The harness failed before the agent did any work. This isn't a task failure, so no attempt is consumed."""


def run_headless(spec: dict) -> dict:
    try:
        out = subprocess.run(spec["cmd"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                             stdin=subprocess.DEVNULL)  # headless agents must never wait on keyboard input
    except FileNotFoundError as e:
        raise InfraError(f"{spec['cmd'][0]} not found: {e}")
    result = {"returncode": out.returncode, "text": out.stdout.strip(), "usd": None, "turns": None, "is_error": False}
    if spec["harness"] == "claude":
        try:
            j = json.loads(out.stdout)
            result.update(text=j.get("result", "") or "", usd=j.get("total_cost_usd"), turns=j.get("num_turns"),
                          is_error=bool(j.get("is_error")))
        except json.JSONDecodeError:
            result["text"] = (out.stdout + out.stderr).strip()
    text = result["text"]
    if out.returncode != 0 or result["is_error"] or not text.strip() or (INFRA_PATTERNS.search(text) and (result["turns"] or 0) <= 1):
        raise InfraError((text or out.stderr or f"exit {out.returncode}").strip()[:300])
    return result


def cmd_start(a) -> dict | None:
    meta, sections = read_task(a.id)
    pending = [t for t in meta.get("depends_on", []) if read_task(t)[0]["status"] != "done"]
    if pending:
        raise SystemExit(f"{a.id} waits on {pending}")
    if meta["status"] not in ("todo", "active"):
        raise SystemExit(f"{a.id} is {meta['status']}; only todo/active tasks can start")
    if meta["status"] == "todo":
        set_status(meta, sections, "active")
    spec = dispatch_spec(meta, "worker", a.headless)
    if not a.exec:
        print(json.dumps(spec, indent=2))
        return None
    if spec["mode"] != "headless":
        raise SystemExit("--exec needs a headless dispatch (use --headless)")
    try:
        res = run_headless(spec)
    except InfraError as e:
        meta, sections = read_task(a.id)
        set_status(meta, sections, "blocked", reason=f"infra: {e}")
        raise SystemExit(f"{a.id} blocked (infrastructure, no attempt used): {e}")
    log("usage", a.id, role="worker", model=meta["tier"].get(spec["harness"], {}).get("model"), usd=res["usd"], turns=res["turns"])
    submit(a.id, res["text"])
    return res


def submit(tid: str, report: str) -> None:
    meta, sections = read_task(tid)
    if meta["status"] != "active":
        raise SystemExit(f"{tid} is {meta['status']}; submit needs active")
    sections["Result"] = report.strip()
    set_status(meta, sections, "verify")


def cmd_submit(a) -> None:
    submit(a.id, read_report(a.report))


def run_gate(cmds: list[str]) -> tuple[bool, str]:
    lines, ok = [], True
    for c in cmds:
        out = subprocess.run(c, cwd=ROOT, shell=True, capture_output=True, text=True)
        tail = (out.stdout + out.stderr).strip().splitlines()[-3:]
        lines.append(f"`{c}` → exit {out.returncode}: " + " | ".join(tail))
        ok = ok and out.returncode == 0
    return ok, "\n".join(lines)


def escalate(meta: dict) -> str:
    """One tier up: effort first (headless), else model (in-process can only change model)."""
    d = decisions()
    in_process = d["options"]["unit"] == "in-process"
    notes = []
    for h, tier in meta["tier"].items():
        eff, models = tier["effort"], MODEL_LADDER[h]
        if not in_process and eff in EFFORT_LADDER and EFFORT_LADDER.index(eff) < EFFORT_LADDER.index("high"):
            tier["effort"] = EFFORT_LADDER[EFFORT_LADDER.index(eff) + 1]
        elif tier["model"] in models and models.index(tier["model"]) < len(models) - 1:
            tier["model"] = models[models.index(tier["model"]) + 1]
        notes.append(f"{h}:{tier['model']}/{tier['effort']}")
    return ", ".join(notes)


def verify(tid: str, verdict: str, report: str, gate: bool = True) -> str:
    d = decisions()
    meta, sections = read_task(tid)
    if meta["status"] != "verify":
        raise SystemExit(f"{tid} is {meta['status']}; verify needs verify")
    gate_note = ""
    if verdict == "pass" and gate and d["options"]["gate"] == "test-based" and meta["shape"] == "change":
        ok, gate_note = run_gate(d["commands"]["test"])
        if not ok:
            verdict = "fail"
            gate_note = "GATE FAILED\n" + gate_note
    sections["Verification"] = (report.strip() + ("\n\n" + gate_note if gate_note else "")).strip()
    if verdict == "pass":
        set_status(meta, sections, "done")
        return "done"
    meta["attempts"] = meta.get("attempts", 0) + 1
    if meta["attempts"] >= 3:
        set_status(meta, sections, "failed", attempts=meta["attempts"])
        return "failed"
    note = escalate(meta) if meta["attempts"] == 2 else ""
    set_status(meta, sections, "active", attempts=meta["attempts"], escalated=note or None)
    return f"active (retry {meta['attempts']}{', escalated to ' + note if note else ''})"


def cmd_verify(a) -> None:
    print(verify(a.id, a.verdict, read_report(a.report), gate=not a.no_gate))


def cmd_block(a) -> None:
    meta, sections = read_task(a.id)
    sections["Result"] = (sections.get("Result", "") + f"\nBlocked: {a.reason}").strip()
    set_status(meta, sections, "blocked", reason=a.reason)


def cmd_reopen(a) -> None:
    """failed/blocked -> active with attempts reset and the tier restored to the roster default (logged)."""
    d = decisions()
    meta, sections = read_task(a.id)
    if meta["status"] not in ("failed", "blocked"):
        raise SystemExit(f"{a.id} is {meta['status']}; only failed/blocked tasks can be reopened")
    role = d["roster"]["worker"]
    override = (d["roster"].get("shapes") or {}).get(meta["shape"], {})
    meta["tier"] = {h: override.get(h, role.get(h)) for h in d["options"]["adapters"] if override.get(h, role.get(h))}
    meta["attempts"] = 0
    sections["Result"], sections["Verification"] = "", ""
    set_status(meta, sections, "active", reopened=a.reason)


def cmd_log_usage(a) -> None:
    log("usage", a.id, role=a.role, model=a.model, usd=a.usd, turns=a.turns)


def cmd_cost(_a) -> None:
    usd, done = {}, {m["id"] for m in all_tasks() if m["status"] == "done"}
    if LOG.is_file():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e["event"] == "usage" and e.get("usd") is not None:
                usd[e["task"]] = usd.get(e["task"], 0) + e["usd"]
    total = sum(usd.values())
    print(f"tasks done: {len(done)} · logged spend: ${total:.2f} · "
          f"cost per completed task: {'$%.2f' % (total / len(done)) if done else 'n/a'}")


def cmd_run_all(a) -> None:
    """Headless pipeline for CI: each ready todo task -> worker -> verifier -> verify. Dry run unless --exec."""
    ran = 0
    for m in all_tasks():
        if ran >= a.max_tasks:
            break
        if m["status"] not in ("todo", "active") or any(read_task(t)[0]["status"] != "done" for t in m.get("depends_on", [])):
            continue
        for _ in range(3):  # at most 3 attempts, matching verify()'s cap
            meta, _s = read_task(m["id"])
            w = dispatch_spec(meta, "worker", headless=True)
            v = dispatch_spec(meta, "verifier", headless=True)
            if not a.exec:
                print(f"{m['id']}: worker   " + shlex.join(w["cmd"]))
                print(f"{m['id']}: verifier " + shlex.join(v["cmd"]))
                break
            cmd_start(argparse.Namespace(id=m["id"], exec=True, headless=True))
            try:
                res = run_headless(v)
            except InfraError as e:
                meta, sections = read_task(m["id"])
                set_status(meta, sections, "blocked", reason=f"infra (verifier): {e}")
                raise SystemExit(f"{m['id']} blocked (infrastructure, no attempt used): {e}")
            log("usage", m["id"], role="verifier", usd=res["usd"], turns=res["turns"])
            verdict = "pass" if re.search(r"^Verdict:\s*pass", res["text"], re.M | re.I) else "fail"
            state = verify(m["id"], verdict, res["text"])
            print(f"{m['id']}: {state}")
            if state in ("done", "failed"):
                break
        ran += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new"); p.add_argument("--shape", choices=["change", "report", "map"], required=True)
    p.add_argument("--title", required=True); p.add_argument("--files", default="")
    p.add_argument("--accept", action="append", default=[]); p.add_argument("--depends", action="append")
    p.add_argument("--brief"); p.add_argument("--usd", type=float, help="per-task budget override")
    p.add_argument("--turns", type=int, help="per-task turn budget override"); p.set_defaults(fn=cmd_new)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("id"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("start"); p.add_argument("id"); p.add_argument("--exec", action="store_true")
    p.add_argument("--headless", action="store_true"); p.set_defaults(fn=cmd_start)
    p = sub.add_parser("submit"); p.add_argument("id"); p.add_argument("--report", required=True); p.set_defaults(fn=cmd_submit)
    p = sub.add_parser("verify"); p.add_argument("id"); p.add_argument("--verdict", choices=["pass", "fail"], required=True)
    p.add_argument("--report", required=True); p.add_argument("--no-gate", action="store_true"); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("block"); p.add_argument("id"); p.add_argument("--reason", required=True); p.set_defaults(fn=cmd_block)
    p = sub.add_parser("reopen"); p.add_argument("id"); p.add_argument("--reason", required=True); p.set_defaults(fn=cmd_reopen)
    p = sub.add_parser("log-usage"); p.add_argument("id"); p.add_argument("--role", required=True)
    p.add_argument("--model"); p.add_argument("--usd", type=float); p.add_argument("--turns", type=int)
    p.set_defaults(fn=cmd_log_usage)
    sub.add_parser("cost").set_defaults(fn=cmd_cost)
    p = sub.add_parser("run-all"); p.add_argument("--exec", action="store_true"); p.add_argument("--max-tasks", type=int, default=10)
    p.set_defaults(fn=cmd_run_all)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    TASKS.mkdir(parents=True, exist_ok=True)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
