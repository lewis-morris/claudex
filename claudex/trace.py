"""A run trace: one line per thing that happened in a job, kept per project in .claudex/trace.jsonl.

It records what costs time and tokens and what goes wrong: every planning, checking and map call (who, which
model, how big the prompt was, how long it took, tokens and cost), every worker step (how big its brief was,
how much memory it was spared, how full its context got, the verdict), the size of each diff and whether it
was cut, and every permission decision. Nothing from the files themselves is kept, only sizes and names.

`claudex trace` summarises it, job by job, with pointers to what looks worth improving. The raw file is
plain JSON lines, so an agent can read it too.
"""
import collections
import datetime
import json
import os
import pathlib

NAME = "trace.jsonl"
KEEP_BYTES = 5_000_000      # past this the file becomes trace.1.jsonl and a new one starts


def write(root, **row):
    """Append one event. Never raises: tracing must never break a job."""
    try:
        path = pathlib.Path(root) / NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > KEEP_BYTES:
            os.replace(path, path.with_name("trace.1.jsonl"))
        line = json.dumps({"at": datetime.datetime.now().isoformat(timespec="milliseconds"), **row},
                          default=str)
        with path.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read(root):
    rows = []
    for name in ("trace.1.jsonl", NAME):
        path = pathlib.Path(root) / name
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def jobs(rows):
    """Events grouped by job, oldest first. Events outside a job (rare) are left out."""
    out = collections.OrderedDict()
    for r in rows:
        if r.get("job"):
            out.setdefault(r["job"], []).append(r)
    return list(out.values())


def _tokens(r):
    return int(r.get("fresh") or 0) + int(r.get("cached") or 0) + int(r.get("output") or 0) + int(r.get("total") or 0)


def summarise(events):
    """One job's numbers."""
    start = next((e for e in events if e["ev"] == "job_start"), {})
    end = next((e for e in reversed(events) if e["ev"] == "job_end"), {})
    calls = [e for e in events if e["ev"] == "call"]
    steps = [e for e in events if e["ev"] == "step"]
    gates = [e for e in events if e["ev"] == "gate"]
    by_role = collections.defaultdict(lambda: {"calls": 0, "secs": 0.0, "tokens": 0, "cost": 0.0, "prompt": 0})
    for c in calls:
        b = by_role[c.get("role") or "?"]
        b["calls"] += 1
        b["secs"] += c.get("secs") or 0
        b["tokens"] += _tokens(c)
        b["cost"] += c.get("cost") or 0
        b["prompt"] += c.get("prompt_chars") or 0
    w = by_role["worker"] if steps else None
    for s in steps:
        w["calls"] += 1
        w["secs"] += s.get("secs") or 0
        w["prompt"] += s.get("brief_chars") or 0
        w["cost"] += s.get("cost") or 0
    return {
        "job": start.get("job") or (events[0].get("job") if events else None),
        "at": start.get("at", events[0]["at"] if events else ""),
        "request": start.get("request", ""),
        "outcome": end.get("outcome", "unfinished"),
        "secs": end.get("secs"),
        "roles": dict(by_role),
        "steps": steps,
        "attempts": len(steps),
        "fixes": sum(1 for s in steps if s.get("verdict") == "fix"),
        "gates": gates,
        "errors": [e for e in events if e["ev"] == "error"]
                  + [c for c in calls if not c.get("ok", True) and c.get("error") != "cancelled"],
        "fallbacks": [c for c in calls if c.get("asked") and c.get("asked") != c.get("agent")],
    }


def hints(summaries):
    """Plain pointers to what looks worth improving, from the numbers. Each says what it saw."""
    out = []
    steps = [s for j in summaries for s in j["steps"]]
    reviewed = [s for s in steps if s.get("verdict") in ("pass", "fix")]
    if reviewed:
        fix_rate = sum(s["verdict"] == "fix" for s in reviewed) / len(reviewed)
        if fix_rate > 0.4 and len(reviewed) >= 3:
            out.append(f"{fix_rate:.0%} of checked steps were sent back for fixes. The plan's instructions may be "
                       "too thin, or the worker's model too light for these jobs.")
    full = [s for s in steps if s.get("context_used") and s.get("context_size")
            and s["context_used"] / s["context_size"] > 0.7]
    if full:
        out.append(f"{len(full)} step(s) ended with the worker's context over 70% full. Long-running worker "
                   "conversations get squashed; starting fresh workers more often would help.")
    cut = [s for s in steps if s.get("diff_truncated")]
    if cut:
        out.append(f"{len(cut)} review(s) got a diff too big to send whole. Smaller steps, or a bigger "
                   "[review] diff_limit, would let the reviewer see everything.")
    gate_calls = [g for j in summaries for g in j["gates"] if g.get("by") == "gatekeeper"]
    if len(gate_calls) >= 10:
        common = collections.Counter(g.get("what", "")[:60] for g in gate_calls).most_common(3)
        out.append(f"The gatekeeper was asked {len(gate_calls)} times. Most asked: "
                   + "; ".join(f"{w} ({n}×)" for w, n in common)
                   + ". A standing allow rule for the safe ones would save the wait.")
    human = [g for j in summaries for g in j["gates"] if g.get("by") == "you"]
    if human:
        waited = sum(g.get("secs") or 0 for g in human)
        out.append(f"You were asked {len(human)} permission question(s), waiting {waited / 60:.1f} min in all.")
    roles = collections.defaultdict(float)
    total = 0.0
    for j in summaries:
        for role, b in j["roles"].items():
            roles[role] += b["secs"]
            total += b["secs"]
    if total:
        slow = [(r, s) for r, s in roles.items() if r != "worker" and s / total > 0.3]
        for r, s in slow:
            out.append(f"'{r}' took {s / total:.0%} of all agent time ({s / 60:.1f} min). Worth a lighter model "
                       "or effort for it, if its answers stay good.")
    big = [(r, b["prompt"] / b["calls"]) for j in summaries for r, b in j["roles"].items()
           if b["calls"] and b["prompt"] / b["calls"] > 80_000]
    if big:
        r, size = max(big, key=lambda x: x[1])
        out.append(f"'{r}' prompts averaged {size / 1000:.0f}k characters. The memory files may want trimming "
                   "(`claudex memory`).")
    flail = [s for s in steps if (s.get("tool_calls") or 0) >= 8
             and (s.get("tool_failures") or 0) / s["tool_calls"] > 0.25]
    if flail:
        common = collections.Counter(a.split(" ")[0] for s in flail for a in s.get("failed_actions") or [])
        out.append(f"{len(flail)} step(s) had over a quarter of the worker's actions fail. The worker may be "
                   "going in circles: a missing tool, a test that can't run, or a permission it keeps hitting."
                   + (" Most failed: " + ", ".join(f"{w} ({n}×)" for w, n in common.most_common(3)) if common else ""))
    stops = [s for s in steps if s.get("stop") in ("max_tokens", "max_turn_requests", "refusal")]
    if stops:
        out.append(f"{len(stops)} step(s) stopped early ({', '.join(sorted({s['stop'] for s in stops}))}).")
    stuck = [s for s in steps if s.get("verdict") in ("stuck", "timed_out")]
    if stuck:
        out.append(f"{len(stuck)} step(s) were stopped by a time limit ("
                   + ", ".join(f"{s.get('agent')} after {(s.get('secs') or 0) / 60:.0f} min, {s['verdict'].replace('_', ' ')}"
                               for s in stuck[:3])
                   + "). 'carry on' resumes the same worker. Steps that keep running this long may want splitting.")
    cut_off = [j for j in summaries if j["outcome"] == "cancelled" and 115 <= (j["secs"] or 0) <= 130]
    if cut_off:
        out.append(f"{len(cut_off)} job(s) were cancelled after about two minutes. That's what Agents of Empires' "
                   "no-progress watchdog does when an agent shows nothing for 120 s; check that the session runs "
                   "current Claudex code (restart it), which shows its planning and checks as running work.")
    chat = [j for j in summaries if j["steps"] and all(s.get("verdict") == "no_changes" for s in j["steps"])
            and "lead" in j["roles"]]
    if len(chat) >= 3:
        cost = sum(j["roles"]["lead"]["cost"] for j in chat)
        out.append(f"{len(chat)} job(s) only answered something but were planned first (lead ${cost:.2f}, "
                   f"{sum(j['roles']['lead']['secs'] for j in chat) / 60:.1f} min). Short plain questions skip planning "
                   "([lead] quick_questions); longer ones that read like questions still get planned.")
    fb = [c for j in summaries for c in j["fallbacks"]]
    if fb:
        out.append(f"{len(fb)} call(s) went to the other agent because one was at its usage limit.")
    errs = [e for j in summaries for e in j["errors"]]
    if errs:
        common = collections.Counter((e.get("role") or e.get("where") or "?", (e.get("error") or "")[:80])
                                     for e in errs).most_common(3)
        out.append("Errors: " + "; ".join(f"{w}: {m} ({n}×)" for (w, m), n in common))
    return out


def report(root, n=5, out=print):
    rows = read(root)
    if not rows:
        out("No trace yet: run a job in this project, then try again.")
        return 0
    summaries = [summarise(e) for e in jobs(rows)][-n:]
    for j in summaries:
        secs = f"{j['secs'] / 60:.1f} min" if j["secs"] else "?"
        out(f"\n{j['at'][:16].replace('T', ' ')} · {j['outcome']} · {secs} · {j['request'][:90]}")
        for role, b in sorted(j["roles"].items(), key=lambda kv: -kv[1]["secs"]):
            cost = f" · ${b['cost']:.2f}" if b["cost"] else ""
            tok = f" · {b['tokens'] / 1000:.0f}k tokens" if b["tokens"] else ""
            out(f"   {role:<15} {b['calls']:>2}× · {b['secs'] / 60:5.1f} min · prompts {b['prompt'] / 1000:.0f}k chars"
                f"{tok}{cost}")
        for s in j["steps"]:
            ctx = (f" · context {s['context_used'] / s['context_size']:.0%}"
                   if s.get("context_used") and s.get("context_size") else "")
            spared = f" · {s['memory_spared'] / 1000:.0f}k memory not re-sent" if s.get("memory_spared") else ""
            acts = (f" · {s['tool_calls']} actions" + (f" ({s['tool_failures']} failed)" if s.get("tool_failures") else "")
                    if s.get("tool_calls") else "")
            out(f"   step {s.get('step')} try {s.get('attempt')}: {s.get('agent')} {s.get('model')} {s.get('effort')}"
                f" → {s.get('verdict')} · {(s.get('secs') or 0) / 60:.1f} min{acts}{ctx}{spared}"
                + (" · diff cut" if s.get("diff_truncated") else ""))
            for a in (s.get("failed_actions") or [])[:3]:
                out(f"      failed: {a}")
        if j["gates"]:
            by = collections.Counter(g.get("by") for g in j["gates"])
            out("   permissions: " + ", ".join(f"{n} by {b}" for b, n in by.most_common()))
    tips = hints(summaries)
    out("\nWorth looking at:" if tips else "\nNothing stands out in these jobs.")
    for t in tips:
        out(f" • {t}")
    out(f"\nThe full trace is {pathlib.Path(root) / NAME}")
    return 0
