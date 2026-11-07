"""claudex bench: race Claude alone, Codex alone and Claudex on the same small projects.

For each task × contender × run:
  1. a fresh copy of the task's starter project (its own git repo, in a throwaway folder)
  2. the contender works on the brief, unattended (Claude: its own auto permission mode; Codex: workspace-write
     sandbox; Claudex: auto mode, nobody to ask, so anything it would ask you is refused)
  3. hidden tests it never saw are run on the result: % passing is the main score
  4. two blind reviewers (Claude Opus and Codex Astra) mark an anonymous copy out of 10
  5. time taken, and tokens from both agents' own logs for that folder (plus Claudex's one-shot calls)
Results go to ~/.local/share/claudex/bench/<when>/ as results.json, report.md and report.html.
"""
import asyncio
import datetime
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid

from . import headless, usage
from .paths import tool_env, which

TASKS = pathlib.Path(__file__).resolve().parent / "bench_tasks"
OUT = usage.DATA / "bench"
WORK = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "claudex/bench"
ARMS = ("claude", "codex", "claudex")
JUDGE_SCHEMA = {"type": "object", "properties": {
    "score": {"type": "integer", "minimum": 1, "maximum": 10}, "correctness": {"type": "string"},
    "quality": {"type": "string"}, "tests": {"type": "string"}, "summary": {"type": "string"}}}
JUDGES = (("claude", "opus", "high"), ("codex", "gpt-6-astra", "high"))
SKIP = {".git", ".claudex", "__pycache__", ".pytest_cache", ".bench_hidden", "node_modules", ".venv"}


def tasks():
    out = {}
    for d in sorted(TASKS.iterdir()):
        if (d / "task.toml").is_file():
            out[d.name] = dict(tomllib.loads((d / "task.toml").read_text()), dir=d)
    return out


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def prepare(task, folder):
    shutil.copytree(task["dir"] / "starter", folder)
    _git(folder, "init", "-q")
    _git(folder, "-c", "user.email=bench@claudex", "-c", "user.name=bench", "add", "-A")
    _git(folder, "-c", "user.email=bench@claudex", "-c", "user.name=bench", "commit", "-qm", "starter")


def command(arm, brief, folder):
    if arm == "claude":
        return ["claude", "-p", brief, "--output-format", "json", "--permission-mode", "auto"]
    if arm == "codex":
        return ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "-C", str(folder), brief]
    me = pathlib.Path(__file__).resolve().parent.parent
    return [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); sys.argv[0] = 'claudex'; "
            "from claudex.__main__ import main; main()", str(me), "-p", brief, "--cwd", str(folder),
            "--mode", "auto", "--unattended"]


RUNNER = r"""
import sys, pytest
project, tests, ini, xml = sys.argv[1:5]
del sys.argv[1:]                  # the contender's code doesn't get told where the results go
sys.path.append(project)          # last, so the project can't stand in for pytest or the standard library
sys.exit(pytest.main([tests, "-c", ini, "--rootdir", tests, "--noconftest", "-p", "no:cacheprovider",
                      "-q", "--tb=line", "--junitxml", xml]))
"""


def hidden_tests(task, folder):
    """Run the task's hidden tests on the result, from a trusted copy outside the project, ignoring any test
    settings, conftest or plugins the contender added. Missing tests count as failures."""
    import xml.etree.ElementTree as ET
    work = WORK / "hidden" / uuid.uuid4().hex[:10]
    tests = work / "tests"
    shutil.copytree(task["dir"] / "hidden", tests)
    ini = work / "pytest.ini"
    ini.write_text("[pytest]\naddopts =\n")
    xml = work / "results.xml"
    expected = int(task.get("expected_tests", 0))
    env = {k: v for k, v in tool_env().items() if not k.startswith("PYTEST")}
    # -E: ignore PYTHON* settings; -P: don't put the current folder first on the import path
    proc = subprocess.Popen([sys.executable, "-E", "-P", "-c", RUNNER, str(folder), str(tests), str(ini), str(xml)],
                            cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            start_new_session=True)
    try:
        proc.communicate(timeout=300)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        _stop_group(proc)
        _stop_in(folder)
        return {"passed": 0, "failed": expected, "total": expected, "pct": 0.0, "note": "hidden tests timed out"}
    _stop_in(folder)
    passed = failed = 0
    try:
        for case in ET.parse(xml).getroot().iter("testcase"):
            if any(child.tag in ("failure", "error", "skipped") for child in case):
                failed += 1
            else:
                passed += 1
    except Exception:
        pass
    total = max(passed + failed, expected)          # tests that never ran (import failed…) count as failed
    failed = total - passed
    note = "" if exit_code in (0, 1) else f"pytest exited {exit_code}"
    return {"passed": passed, "failed": failed, "total": total,
            "pct": round(100 * passed / total, 1) if total else 0.0, "note": note}


def anonymous_copy(folder):
    dest = WORK / "blind" / uuid.uuid4().hex[:10]
    shutil.copytree(folder, dest, ignore=lambda d, names: [n for n in names if n in SKIP])
    return dest


async def judge(task, blind):
    prompt = ("You are marking a small coding job, blind: you don't know who or what wrote it. Read the project "
              "files (read-only). Mark it out of 10 against the brief for: correctness (does it do exactly what was "
              "asked, edge cases included), quality (clear, simple, safe code; sensible structure), and tests "
              "(meaningful, would catch mistakes). 10 = excellent and complete; 5 = works but with clear gaps; "
              "1 = broken or missing. Be strict and consistent.\n\nTHE BRIEF:\n" + task["brief"])
    scores = []
    for agent, model, effort in JUDGES:
        try:
            v = await headless.ask(agent, str(blind), prompt, JUDGE_SCHEMA, model, effort, 900, role="bench judge")
            scores.append({"judge": f"{agent} {model}", **v})
        except Exception as e:
            scores.append({"judge": f"{agent} {model}", "score": None, "summary": f"couldn't mark: {str(e)[:120]}"})
    return scores


def tokens(folder, start_iso, end_iso):
    """Tokens both agents logged for this folder in this window, plus Claudex's own one-shot calls."""
    root = str(folder)
    since = datetime.datetime.fromisoformat(start_iso).timestamp() - 60
    rows = [e for e in usage.native(since_epoch=since) if str(e.get("project", "")).startswith(root)
            and start_iso <= e.get("at", "") <= end_iso]
    rows += [r for r in usage.ledger(start_iso) if str(r.get("project", "")).startswith(root)
             and r.get("role") != "bench judge"]
    out = {"claude": 0, "codex": 0}
    for r in rows:
        if r.get("agent") in out:
            work = int(r.get("fresh") or 0) + int(r.get("output") or 0)
            out[r["agent"]] += work or int(r.get("total") or 0)
    out["total"] = out["claude"] + out["codex"]
    return out


def _stop_group(proc):
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        for _ in range(50):
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            continue
        break
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _stop_in(folder):
    """Stop every process still working in this folder. Claudex's workers run in their own process groups, so
    stopping the contender's group isn't enough; anything whose working folder is inside it is ours to stop."""
    import signal
    root = str(pathlib.Path(folder).resolve())
    me = os.getpid()
    for attempt in range(3):
        found = []
        for d in pathlib.Path("/proc").iterdir():
            if not d.name.isdigit() or int(d.name) == me:
                continue
            try:
                cwd = os.readlink(d / "cwd")
            except OSError:
                continue
            if cwd == root or cwd.startswith(root + "/"):
                found.append(int(d.name))
        if not found:
            return
        for pid in found:
            try:
                os.kill(pid, signal.SIGTERM if attempt == 0 else signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(1)


def run_one(task_name, task, arm, n, stamp, timeout_min, judge_it):
    folder = WORK / stamp / f"{task_name}-{arm}-{n}"
    prepare(task, folder)
    if arm != "claudex" and not which(arm):
        return {"task": task_name, "arm": arm, "run": n, "error": f"{arm} isn't installed"}
    print(f"  {task_name} · {arm} · run {n}: working…", flush=True)
    start_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    t0 = time.time()
    proc = subprocess.Popen(command(arm, task["brief"], folder), cwd=folder, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=tool_env(), stdin=subprocess.DEVNULL,
                            start_new_session=True)   # its own group: everything it starts can be stopped
    try:
        out, _ = proc.communicate(timeout=timeout_min * 60)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        out, exit_code = "", "timeout"
    _stop_group(proc)                                  # nothing may keep working (or spending) during scoring
    _stop_in(folder)
    secs = round(time.time() - t0, 1)
    time.sleep(2)   # let the agents finish writing their logs
    end_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    cost = None
    if arm == "claude":
        try:
            cost = json.loads(out).get("total_cost_usd")
        except Exception:
            pass
    result = {"task": task_name, "arm": arm, "run": n, "secs": secs, "exit": exit_code, "folder": str(folder),
              "hidden": hidden_tests(task, folder), "tokens": tokens(folder, start_iso, end_iso), "cost_usd": cost}
    if judge_it:
        result["review"] = asyncio.run(judge(task, anonymous_copy(folder)))
        marks = [r["score"] for r in result["review"] if isinstance(r.get("score"), int)]
        result["review_score"] = round(sum(marks) / len(marks), 1) if marks else None
    h = result["hidden"]
    print(f"    tests {h['passed']}/{h['total']} · review {result.get('review_score', '–')} · {secs:.0f}s · "
          f"{usage.human(result['tokens']['total'])} tokens", flush=True)
    return result


def summarise(results):
    by = {}
    for r in results:
        if "error" in r:
            continue
        a = by.setdefault((r["task"], r["arm"]), {"runs": 0, "pct": 0, "secs": 0, "tokens": 0, "review": [], "cost": []})
        a["runs"] += 1
        a["pct"] += r["hidden"]["pct"]
        a["secs"] += r["secs"]
        a["tokens"] += r["tokens"]["total"]
        if r.get("review_score") is not None:
            a["review"].append(r["review_score"])
        if r.get("cost_usd") is not None:
            a["cost"].append(r["cost_usd"])
    rows = []
    for (task, arm), a in sorted(by.items()):
        n = a["runs"]
        rows.append({"task": task, "arm": arm, "runs": n, "tests_pct": round(a["pct"] / n, 1),
                     "review": round(sum(a["review"]) / len(a["review"]), 1) if a["review"] else None,
                     "minutes": round(a["secs"] / n / 60, 1), "tokens": round(a["tokens"] / n)})
    return rows


def report(stamp, results, rows):
    d = OUT / stamp
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(json.dumps({"results": results, "summary": rows}, indent=1))
    md = [f"# Claudex bench, {stamp}", "", "| Task | Contender | Runs | Hidden tests passed | Review /10 | Minutes | Tokens |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['task']} | {r['arm']} | {r['runs']} | {r['tests_pct']}% | {r['review'] or '–'} | "
                  f"{r['minutes']} | {usage.human(r['tokens'])} |")
    md += ["", "Hidden tests were never shown to the contenders. Reviews are blind (two reviewers, averaged). "
           "Tokens = new input + output from both agents' logs for that folder, plus Claudex's own calls.", ""]
    for r in results:
        if r.get("review"):
            md.append(f"**{r['task']} · {r['arm']} · run {r['run']}**: " + "; ".join(
                f"{v['judge']} {v.get('score')}/10: {v.get('summary', '')[:200]}" for v in r["review"]))
    (d / "report.md").write_text("\n".join(md) + "\n")
    colour = {"claude": "#ff8f6b", "codex": "#5fd0e0", "claudex": "#ffd23f"}
    metrics = (("tests_pct", "Hidden tests passed (%)", 100), ("review", "Blind review (/10)", 10),
               ("minutes", "Minutes", None), ("tokens", "Tokens", None))
    cards = []
    for task in sorted({r["task"] for r in rows}):
        tr = [r for r in rows if r["task"] == task]
        blocks = []
        for key, label, top in metrics:
            peak = top or max([r[key] or 0 for r in tr] + [1])
            bars = "".join(
                f'<div class="bar"><span class="who">{r["arm"]}</span><span class="track"><span class="fill" '
                f'style="width:{100 * (r[key] or 0) / peak:.1f}%;background:{colour[r["arm"]]}"></span></span>'
                f'<span class="val">{usage.human(r[key]) if key == "tokens" else (r[key] if r[key] is not None else "–")}'
                f'</span></div>' for r in tr)
            blocks.append(f'<div class="metric"><h4>{label}</h4>{bars}</div>')
        cards.append(f'<section class="card"><h3>{html.escape(task)}</h3><div class="grid">{"".join(blocks)}</div></section>')
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claudex bench</title><style>
:root{{--bg:#13201a;--panel:#1a2a22;--edge:#2e4a3c;--text:#eef3e6;--dim:#8fa597}}
body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,sans-serif;padding:16px}}main{{max-width:1000px;margin:auto}}
.card{{background:var(--panel);border:1px solid var(--edge);border-radius:12px;padding:14px 16px;margin:14px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}h4{{margin:4px 0;color:var(--dim);font-weight:500}}
.bar{{display:grid;grid-template-columns:64px 1fr 56px;align-items:center;gap:8px;margin:5px 0}}.track{{background:var(--edge);border-radius:6px;height:14px;overflow:hidden}}
.fill{{display:block;height:100%;border-radius:6px}}.val{{text-align:right;font-variant-numeric:tabular-nums}}.dim{{color:var(--dim)}}
</style></head><body><main><h1>🐒 Claudex bench</h1><p class="dim">{stamp} · hidden tests never shown to the contenders ·
blind reviews by Claude Opus and Codex Astra, averaged · tokens from both agents' own logs</p>{''.join(cards)}</main></body></html>"""
    (d / "report.html").write_text(page)
    return d


def main(task_names=None, arms=ARMS, runs=1, judge_it=True, timeout_min=30):
    all_tasks = tasks()
    if subprocess.run([sys.executable, "-E", "-P", "-c", "import pytest"], capture_output=True).returncode != 0:
        print(f"The benchmark marks work with pytest, which this Python ({sys.executable}) doesn't have. "
              f"Install it: {sys.executable} -m pip install --user pytest")
        return 1
    chosen = [t for t in (task_names or list(all_tasks)) if t in all_tasks]
    if not chosen:
        print("No such task. Available: " + ", ".join(all_tasks))
        return 1
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"Claudex bench {stamp}: {len(chosen)} task(s) × {len(arms)} contender(s) × {runs} run(s)\n")
    results = []
    for n in range(1, runs + 1):
        for t in chosen:
            for arm in arms:
                results.append(run_one(t, all_tasks[t], arm, n, stamp, timeout_min, judge_it))
    rows = summarise(results)
    d = report(stamp, results, rows)
    print("\n" + (d / "report.md").read_text().split("\n\nHidden tests")[0])
    print(f"\nReport: {d / 'report.html'}")
    return 0
