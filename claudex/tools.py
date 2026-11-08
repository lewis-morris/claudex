"""claudex doctor | rules | log | update."""
import argparse
import asyncio
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

from . import headless
from .engine import CATALOG, load_rules
from .paths import DEFAULT_RULES, USER_RULES, rules_file, which

OK, WARN, BAD = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m"
if not sys.stdout.isatty():
    OK, WARN, BAD = "OK  ", "WARN", "FAIL"
CLAUDE_ALIASES = {"opus", "sonnet", "haiku", "fable", "default", "best", "opusplan"}
CONNECTOR_PACKAGES = ["@agentclientprotocol/claude-agent-acp", "@agentclientprotocol/codex-acp"]
AOE_CONFIG = pathlib.Path.home() / ".config/agent-of-empires/config.toml"


def _run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=headless._env())
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def login_state():
    """{"claude": (signed_in, detail), "codex": (signed_in, detail)}"""
    out = {}
    code, text = _run(["claude", "auth", "status"])
    try:
        st = json.loads(text)
        out["claude"] = (bool(st.get("loggedIn")), st.get("authMethod") or "not signed in")
    except Exception:
        out["claude"] = (False, (text.splitlines() or ["claude not found"])[-1][:80])
    code, text = _run(["codex", "login", "status"])
    last = (text.splitlines() or ["codex not found"])[-1]
    out["codex"] = (code == 0 and "Logged in" in text, last[:80])
    return out


def login(which=None, device=False, again=False):
    """Sign in to whichever of Claude and Codex isn't signed in (or the one named), in this terminal."""
    if not sys.stdin.isatty():
        print("claudex login needs a terminal you can type in (it opens each sign-in).")
        return 1
    commands = {"claude": ["claude", "auth", "login"],
                "codex": ["codex", "login"] + (["--device-auth"] if device else [])}
    before = login_state()
    ran = False
    for agent in (which,) if which else ("claude", "codex"):
        ok, detail = before[agent]
        if ok and not again:
            print(f" {OK} {agent.title()}: already signed in ({detail})")
            continue
        print(f"\nSigning in to {agent.title()}… (follow the steps it shows; a browser may open)\n")
        subprocess.call(commands[agent], env=headless._env())
        ran = True
    if not ran:
        return 0
    print()
    after, bad = login_state(), 0
    for agent in ("claude", "codex"):
        ok, detail = after[agent]
        bad += not ok
        print(f" {OK if ok else BAD} {agent.title()}: {'signed in' if ok else 'NOT signed in'} ({detail})")
    if bad:
        print("\nClaudex will stop rather than guess if a job needs the agent that isn't signed in. "
              "Try again with `claudex login " + ("codex --device" if not after["codex"][0] else "claude") + "`.")
    return 1 if bad else 0


def used_models(rules):
    """Every (agent, model, effort) the rules can ask for, with where it's used."""
    out = {}

    def add(agent, model, effort, where):
        out.setdefault((agent, model, effort), []).append(where)
    lead = rules["lead"]
    add(lead.get("agent", "claude"), lead["model"], lead["effort"], "lead")
    d = rules["defaults"]
    add(d["agent"], d["model"], d["effort"], "default")
    for a, v in rules["agent_defaults"].items():
        add(a, v["model"], v["effort"], f"{a} only")
    for r in rules.get("rule", []):
        u = r["use"]
        add(u["agent"], u["model"], u["effort"], r["when"][:40])
    for a, v in rules.get("reviewers", {}).items():
        add(a, v["model"], v["effort"], "reviewer")
    for a, rungs in rules.get("ladder", {}).items():
        for m, e in rungs:
            add(a, m, e, "step-up ladder")
    m = rules.get("memory", {}).get("map_model")
    if m:
        add(m["agent"], m["model"], m["effort"], "project map")
    g = rules.get("permissions", {}).get("gatekeeper")
    if g:
        add(g.get("agent", "claude"), g.get("model", "sonnet"), g.get("effort", "low"), "gatekeeper")
    return out


def codex_catalog():
    try:
        d = json.loads((pathlib.Path.home() / ".codex/models_cache.json").read_text())
        models = d.get("models", d) if isinstance(d, dict) else d
        return {m.get("slug") or m.get("id"): [x.get("effort") for x in m.get("supported_reasoning_levels") or []]
                for m in models}
    except Exception:
        return {}


def doctor(live=False, cwd=None, models_file=None):
    problems = 0
    unverified = set()

    def line(mark, text):
        nonlocal problems
        if mark == BAD:
            problems += 1
        print(f" {mark} {text}")

    print("Logins and versions")
    code, out = _run(["claude", "--version"])
    line(OK if code == 0 else BAD, f"Claude Code {out.splitlines()[0] if out else '(not found)'}")
    code, out = _run(["claude", "auth", "status"])
    try:
        st = json.loads(out)
        exp = st.get("loginExpiresAt")
        days = (datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                - datetime.datetime.now(datetime.timezone.utc)).days if exp else None
        if not st.get("loggedIn"):
            line(BAD, "Claude: not signed in. Run: claudex login")
        elif days is not None and days < 7:
            line(WARN, f"Claude: logged in ({st.get('authMethod')}), but the login ends in {days} days")
        else:
            line(OK, f"Claude: logged in ({st.get('authMethod')})" + (f", {days} days left" if days else ""))
    except Exception:
        line(BAD, f"Claude: couldn't read login status ({out[:120]})")
    code, out = _run(["codex", "--version"])
    line(OK if code == 0 else BAD, f"Codex {out.splitlines()[-1] if out else '(not found)'}")
    code, out = _run(["codex", "login", "status"])
    line(OK if code == 0 and "Logged in" in out else BAD,
         f"Codex: {out.splitlines()[-1] if out else 'unknown'}" + ("" if "Logged in" in out else ". Run: claudex login"))

    print("\nRules")
    try:
        from .engine import repo_root
        start = cwd or os.getcwd()
        here = repo_root(start) or start
        rules = load_rules(project_root=here, models_file=models_file)   # exactly what a job there would use
        line(OK, f"{rules_file()}" + ("  (the defaults)" if rules_file() == DEFAULT_RULES else ""))
        if rules.get("_models_file"):
            line(OK, f"who does what: {rules['_models_file']}")
    except Exception as e:
        line(BAD, f"{rules_file()} won't load: {e}")
        return 1

    print("\nWorker connectors")
    for name, cmd in rules["agents"].items():
        exe = which(cmd[0])
        hint = {"claude-agent-acp": "npm i -g @agentclientprotocol/claude-agent-acp",
                "codex-acp": "npm i -g @agentclientprotocol/codex-acp"}.get(cmd[0], "")
        line(OK if exe else BAD, f"{name}: {exe or cmd[0] + ' not found. Install: ' + hint}")

    print("\nModels")
    cx = codex_catalog()
    try:
        opts = json.loads(CATALOG.read_text())["agents"]["claude"]["options"]
        aoe_claude = [v["value"] for o in opts if o.get("id") == "model" for v in o["options"]]
    except Exception:
        aoe_claude = []
    uses = used_models(rules)
    for (agent, model, effort), where in sorted(uses.items()):
        label = f"{agent} {model} {effort}  ({', '.join(sorted(set(where)))[:70]})"
        if agent == "codex":
            if not cx:
                line(WARN, f"{label}: no Codex model list cached yet")
            elif model not in cx:
                line(BAD, f"{label}: Codex doesn't offer {model}")
            elif cx[model] and effort not in cx[model]:
                line(BAD, f"{label}: {model} doesn't support {effort}")
            else:
                line(OK, label)
        else:
            if model in CLAUDE_ALIASES or model in aoe_claude:
                line(OK, label)
            elif model.startswith("claude-"):
                unverified.add((agent, model))
                line(WARN, f"{label}: can't confirm this exact name offline (doctor --live checks it)")
            else:
                line(BAD, f"{label}: unknown Claude model")

    print("\nAgents of Empires")
    try:
        text = AOE_CONFIG.read_text()
        line(OK if 'claudex = "' in text else WARN,
             "claudex is registered" if 'claudex = "' in text else "claudex isn't registered as an agent")
    except Exception:
        line(WARN, "no Agents of Empires config found")
    from .paths import stale_workers
    stale = stale_workers()
    if stale:
        for pid, sid, mins in stale:
            line(WARN, f"a Claudex session started {mins} min ago is running older code"
                       + (f": restart it with `aoe acp stop {sid}` (it comes back on your next message)" if sid
                          else f" (process {pid}): restart it"))
    else:
        line(OK, "every running Claudex session is on the current code")

    if live:
        print("\nLive check (a tiny message to each model)")
        seen = {}
        for (agent, model, _effort) in uses:
            seen.setdefault((agent, model), True)
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        for agent, model in sorted(seen):
            t = time.time()
            try:
                asyncio.run(headless.ask(agent, os.getcwd(), "Return ok=true.", schema, model, "low",
                                         timeout=240, read_files=False))
                line(OK, f"{agent} {model} answered in {time.time() - t:.0f}s")
                unverified.discard((agent, model))
            except Exception as e:
                line(BAD, f"{agent} {model}: {str(e)[:160]}")
    if unverified:
        problems += len(unverified)
        print(f"\n {WARN} Not verified: " + ", ".join(m for _, m in sorted(unverified))
              + ". Run `claudex doctor --live` to check them.")
    print("\n" + ("All good." if not problems else f"{problems} problem(s) found."))
    return 1 if problems else 0


def show_rules(edit=False):
    if edit:
        target = rules_file()
        if target == DEFAULT_RULES:   # never edit the shipped defaults: make your own copy first
            USER_RULES.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DEFAULT_RULES, USER_RULES)
            target = USER_RULES
            print(f"Made your own copy of the rules at {USER_RULES}")
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        return subprocess.call([editor, str(target)])
    r = load_rules()
    lead = r["lead"]
    print(f"Rules file: {rules_file()}  (re-read on every message; `claudex rules --edit` to change)\n")
    print(f"Lead:        {lead.get('agent', 'claude')} {lead['model']} {lead['effort']}")
    d = r["defaults"]
    print(f"Default:     {d['agent']} {d['model']} {d['effort']}")
    for a, v in r.get("reviewers", {}).items():
        print(f"Reviewer:    {a} {v['model']} {v['effort']}  (checks the other side's work)")
    print("\nWho does what:")
    for x in r.get("rule", []):
        u = x["use"]
        extra = " + browser" if u.get("browser") else ""
        print(f"  {u['agent']:6} {u['model']:12} {u['effort']:7}{extra}  {x['when']}")
    print("\nStep-up ladder (when a review keeps failing):")
    for a, rungs in r.get("ladder", {}).items():
        print(f"  {a}: " + " → ".join(f"{m} {e}" for m, e in rungs))
    return 0


def show_log(cwd, n):
    from .engine import repo_root
    rows = []
    root = repo_root(cwd) or cwd                 # Claudex keeps one memory per repository
    for f in (pathlib.Path(root) / ".claudex" / "sessions").glob("*/steps.jsonl"):
        for l in f.read_text().splitlines():
            try:
                rows.append({**json.loads(l), "_session": f.parent.name})
            except Exception:
                pass
    rows.sort(key=lambda r: r.get("at", ""))
    if not rows:
        print("No Claudex steps in this project yet.")
        return 0
    for r in rows[-n:]:
        if r.get("kind") == "step":
            print(f"{r['at']}  step {r['step']} · {r['agent']} {r['model']} {r['effort']} · try {r.get('attempt', 1)}"
                  f" · {r.get('secs', '?')}s · {r.get('verdict', r.get('stop'))} · {r['title'][:50]}")
        else:
            print(f"{r['at']}  {r.get('kind')} · {r.get('verdict')} {r.get('reviewer', '')}")
    return 0


def _self_update():
    """Update Claudex itself: `git pull` for a checkout, `uv tool upgrade` for an install."""
    from .paths import PACKAGE
    root = PACKAGE.parent
    if (root / ".git").exists():
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            return ["echo", f"Claudex at {root} has uncommitted changes, so it wasn't pulled"]
        return ["git", "-C", str(root), "pull", "--ff-only"]
    where = str(PACKAGE).replace("\\", "/")
    if "/uv/tools/claudex/" in where and shutil.which("uv"):
        return ["uv", "tool", "upgrade", "claudex"]
    if "/pipx/venvs/claudex/" in where and shutil.which("pipx"):
        return ["pipx", "upgrade", "claudex"]
    return ["echo", f"Claudex at {where}: update it the way you installed it"]


def update():
    steps = [_self_update(), ["claude", "update"], ["codex", "update"],
             ["npm", "update", "-g", *CONNECTOR_PACKAGES]]
    for cmd in steps:
        print(f"\n$ {' '.join(cmd)}")
        if subprocess.call(cmd, env=headless._env()) != 0:
            print(f"(that didn't finish cleanly; carrying on)")
    print()
    return doctor()


def main(argv):
    ap = argparse.ArgumentParser(prog="claudex")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check logins, versions, connectors and models")
    d.add_argument("--live", action="store_true", help="also send a tiny message to every model")
    d.add_argument("--cwd", default=None, help="check the setup a job in this folder would use")
    d.add_argument("--models", metavar="FILE", help="check this models.md")
    r = sub.add_parser("rules", help="show who does what")
    r.add_argument("--edit", action="store_true")
    tr = sub.add_parser("trace", help="how recent jobs ran: time, tokens and cost per role, and what to improve")
    tr.add_argument("-n", type=int, default=5, help="how many recent jobs (default 5)")
    tr.add_argument("--cwd", default=".")
    tr.add_argument("--path", action="store_true", help="just print where the trace file is")
    lg = sub.add_parser("log", help="recent steps in this project")
    lg.add_argument("-n", type=int, default=20)
    lg.add_argument("--cwd", default=".")
    sub.add_parser("update", help="update Claude, Codex and the connectors, then run doctor")
    ini = sub.add_parser("init", help="set this project up: one house-rules file for both agents, memory, map")
    ini.add_argument("--yes", action="store_true", help="don't ask before changing CLAUDE.md / AGENTS.md")
    ini.add_argument("--no-map", action="store_true", help="skip building the first project map")
    ini.add_argument("--cwd", default=".")
    sh = sub.add_parser("shared", help="what Claude and Codex share: house rules, skills, connectors")
    sh.add_argument("--link-skills", action="store_true", help="make every skill available to both (links)")
    sh.add_argument("--yes", action="store_true")
    sh.add_argument("--cwd", default=".")
    me = sub.add_parser("memory", help="this project's Claudex memory: files, map, decisions, recent jobs")
    me.add_argument("--files", action="store_true", help="just list every file")
    me.add_argument("--cwd", default=".")
    pe = sub.add_parser("permissions", help="the permission rules in force and recent decisions")
    pe.add_argument("--edit", action="store_true", help="edit this project's own rules")
    pe.add_argument("-n", type=int, default=30)
    pe.add_argument("--cwd", default=".")
    us = sub.add_parser("usage", help="tokens across Claude and Codex, as a chart (or --web for a live page)")
    us.add_argument("--days", type=int, default=14)
    us.add_argument("--project", help="just this folder")
    us.add_argument("--web", action="store_true", help="open a live page in your browser")
    us.add_argument("--lan", action="store_true", help="with --web: also reachable from other devices on your network")
    us.add_argument("--port", type=int, default=8765)
    us.add_argument("--no-open", action="store_true", help="with --web: don't open the browser")
    us.add_argument("--json", action="store_true", help="raw numbers")
    mo = sub.add_parser("models", help="who does what, and where it comes from (models.md)")
    mo.add_argument("--init", action="store_true", help="make an editable models.md from the rules in force")
    mo.add_argument("--edit", action="store_true", help="open models.md (made first if needed)")
    mo.add_argument("--global", dest="global_", action="store_true", help="your models.md for every project")
    mo.add_argument("--models", metavar="FILE", help="show what this models.md would do")
    mo.add_argument("--cwd", default=".")
    be = sub.add_parser("bench", help="race Claude, Codex and Claudex on small projects")
    be.add_argument("--tasks", help="comma-separated (default: all)")
    be.add_argument("--arms", default="claude,codex,claudex", help="who races (default: all three)")
    be.add_argument("--runs", type=int, default=1, help="runs per task and contender (results vary: 2+ is fairer)")
    be.add_argument("--no-judge", action="store_true", help="skip the blind reviews")
    be.add_argument("--timeout", type=int, default=30, help="minutes per run")
    be.add_argument("--list", action="store_true", help="list the tasks")
    li = sub.add_parser("login", help="sign in to Claude and/or Codex (only the ones that need it)")
    li.add_argument("agent", nargs="?", choices=["claude", "codex"], help="just this one")
    li.add_argument("--device", action="store_true", help="Codex: sign in with a code (for a remote machine)")
    li.add_argument("--again", action="store_true", help="sign in again even if already signed in")
    a = ap.parse_args(argv)
    if a.cmd == "doctor":
        return doctor(a.live, a.cwd, a.models)
    if a.cmd == "rules":
        return show_rules(a.edit)
    if a.cmd == "log":
        return show_log(a.cwd, a.n)
    if a.cmd == "trace":
        from . import trace
        from .engine import repo_root
        root = pathlib.Path(repo_root(a.cwd) or a.cwd) / ".claudex"
        if a.path:
            print(root / trace.NAME)
            return 0
        return trace.report(root, a.n)
    if a.cmd == "login":
        return login(a.agent, a.device, a.again)
    if a.cmd == "bench":
        from . import bench
        if a.list:
            for name, t in bench.tasks().items():
                print(f"{name:<14} {t['title']}")
            return 0
        arms = tuple(x.strip() for x in a.arms.split(",") if x.strip() in bench.ARMS)
        return bench.main(a.tasks.split(",") if a.tasks else None, arms, a.runs, not a.no_judge, a.timeout)
    from . import admin
    if a.cmd == "models":
        return admin.show_models(a.cwd, a.init, a.edit, a.global_, a.models)
    if a.cmd == "init":
        return admin.init(a.cwd, a.yes, a.no_map)
    if a.cmd == "shared":
        return admin.shared(a.cwd, a.link_skills, a.yes)
    if a.cmd == "memory":
        return admin.memory(a.cwd, a.files)
    if a.cmd == "permissions":
        return admin.show_permissions(a.cwd, a.n, a.edit)
    if a.cmd == "usage":
        return admin.show_usage(a.days, a.project, a.web, a.lan, a.port, a.json, a.no_open)
    return update()
