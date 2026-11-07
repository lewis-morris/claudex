"""claudex init | shared | memory | permissions | usage: set a project up, and see what's going on."""
import datetime
import json
import os
import pathlib
import subprocess
import sys

from . import permissions as perms
from . import usage
from .engine import MODES, load_rules, repo_root
from .paths import USER_RULES, rules_file, tool_env, which

TTY = sys.stdout.isatty()
B, D, G, R, Y, RESET = (("\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m") if TTY else ("",) * 6)
IMPORT_LINE = "@AGENTS.md"
AGENTS_TEMPLATE = """# House rules for AI agents

Claude Code and Codex both follow this file (CLAUDE.md just points here).

## The project
- What it is: (one or two lines)
- How to run it:
- How to test it:

## Rules
- Run the tests before handing work over.
- Don't commit or push unless asked.
"""
PROJECT_RULES_TEMPLATE = """# This project's own Claudex rules, layered over your main rules (`claudex rules`).
# Anything here wins for this project only. Examples:
#
# [permissions]
# allow = ["run:npm run dev*", "run:./manage.py test*"]
# ask = ["run:./deploy.sh*"]
#
# [defaults]
# agent = "claude"
"""


def _root(cwd):
    return pathlib.Path(repo_root(cwd) or cwd).resolve()


def _ask_yes(question, yes):
    if yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question} (not asking: no terminal; run again with --yes)")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


# ---------- init ----------
def house_rules_plan(root):
    """What `init` would do with CLAUDE.md and AGENTS.md: [(file, action, new text or None)]."""
    claude, agents = root / "CLAUDE.md", root / "AGENTS.md"
    c = claude.read_text(errors="replace") if claude.is_file() else None
    a = agents.read_text(errors="replace") if agents.is_file() else None
    if c is not None and IMPORT_LINE in c and a is not None:
        return []                                      # already one shared file
    plan = []
    if a is None and c is None:
        plan.append((agents, "create (a starting point for you to fill in)", AGENTS_TEMPLATE))
    elif a is None:
        plan.append((agents, "create from CLAUDE.md's rules", c))
    elif c is not None and c.strip() != a.strip():
        merged = a.rstrip() + "\n\n## Also from CLAUDE.md\n\n" + c.strip() + "\n"
        plan.append((agents, "add CLAUDE.md's rules at the end", merged))
    plan.append((claude, "point at AGENTS.md (Claude reads it through this)" if c is None
                 else "replace with a pointer to AGENTS.md (old copy kept as CLAUDE.md.bak)",
                 f"{IMPORT_LINE}\n\n<!-- One set of house rules for Claude and Codex lives in AGENTS.md. "
                 "Claude-only notes can go below this line. -->\n"))
    return plan


def init(cwd=".", yes=False, no_map=False):
    root = _root(cwd)
    print(f"{B}Setting up Claudex in {root}{RESET}\n")
    plan = house_rules_plan(root)
    if plan:
        print("House rules (one file both agents follow):")
        for f, action, _ in plan:
            print(f"  {f.name}: {action}")
        if _ask_yes("Make these changes?", yes):
            for f, action, text in plan:
                if f.name == "CLAUDE.md" and f.exists():
                    f.with_name("CLAUDE.md.bak").write_text(f.read_text())
                f.write_text(text)
            print(f"  {G}done{RESET} (not committed: commit them when you're happy)")
    else:
        print(f"House rules: {G}already shared{RESET} (CLAUDE.md points at AGENTS.md)")
    mem = root / ".claudex" / "project"
    mem.mkdir(parents=True, exist_ok=True)
    print(f"Memory folder: {mem.parent}")
    try:
        rel = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=root, capture_output=True,
                             text=True, timeout=10).stdout.strip()
        if rel:
            ex = root / rel
            text = ex.read_text() if ex.exists() else ""
            if ".claudex/" not in text.split():
                ex.parent.mkdir(parents=True, exist_ok=True)
                ex.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".claudex/\n")
            print("  kept out of git (.git/info/exclude)")
    except Exception:
        pass
    own = root / ".claudex" / "rules.toml"
    if not own.exists():
        own.write_text(PROJECT_RULES_TEMPLATE)
        print(f"Project rules: {own} (empty for now; anything you add there wins for this project)")
    if not no_map and not (mem / "map.md").exists():
        print("Project map: asking a quick model to look round the project…", flush=True)
        try:
            import asyncio
            from . import headless
            from .engine import MAP_SCHEMA
            rules = load_rules(project_root=root)
            m = rules.get("memory", {}).get("map_model", {"agent": "codex", "model": "gpt-6.1-sol", "effort": "low"})
            v = asyncio.run(headless.ask(m["agent"], str(root), "Write a project map: a short guide (under 150 "
                                         "lines, markdown) for AI agents to this codebase: layout, key files and "
                                         "what they do, data/database, how it runs, tests and deploys. Read the "
                                         "files you need.", MAP_SCHEMA, m["model"], m["effort"], 600, role="map"))
            (mem / "map.md").write_text(v["map"].rstrip() + "\n")
            print(f"  {G}written{RESET}: {mem / 'map.md'}")
        except Exception as e:
            print(f"  {Y}skipped{RESET}: {str(e)[:120]} (Claudex makes one after the first job anyway)")
    print(f"\n{B}Ready.{RESET} Run `claudex` here. `claudex shared` shows what both agents share.")
    return 0


# ---------- shared ----------
def _skills():
    out = {}
    for agent, root in (("claude", pathlib.Path.home() / ".claude/skills"),
                        ("codex", pathlib.Path.home() / ".codex/skills")):
        if root.is_dir():
            for d in root.iterdir():
                if (d / "SKILL.md").is_file() and not d.name.endswith("-workspace"):
                    out.setdefault(d.name, {})[agent] = d
    return out


def _mcp_servers():
    claude, codex = set(), set()
    try:
        data = json.loads((pathlib.Path.home() / ".claude.json").read_text())
        claude |= set((data.get("mcpServers") or {}).keys())
        for proj in (data.get("projects") or {}).values():
            claude |= set((proj or {}).get("mcpServers", {}).keys())
    except Exception:
        pass
    try:
        import tomllib
        cfg = tomllib.loads((pathlib.Path.home() / ".codex/config.toml").read_text())
        codex |= set((cfg.get("mcp_servers") or {}).keys())
    except Exception:
        pass
    return claude, codex


def shared(cwd=".", link_skills=False, yes=False):
    root = _root(cwd)
    print(f"{B}What Claude and Codex share{RESET}  ({root.name})\n")
    plan = house_rules_plan(root)
    if not plan:
        print(f" {G}✓{RESET} House rules: one file (AGENTS.md); CLAUDE.md points at it")
    else:
        print(f" {Y}!{RESET} House rules: not shared yet. Claude reads CLAUDE.md, Codex reads AGENTS.md. "
              "Run `claudex init` to make it one file. (Inside Claudex both are always shown to everyone.)")
    sk = _skills()
    both = sorted(n for n, v in sk.items() if len(v) == 2)
    c_only = sorted(n for n, v in sk.items() if list(v) == ["claude"])
    x_only = sorted(n for n, v in sk.items() if list(v) == ["codex"])
    print(f"\n Skills: {len(both)} both · {len(c_only)} Claude only · {len(x_only)} Codex only")
    if c_only:
        print(f"   Claude only: {', '.join(c_only[:30])}{' …' if len(c_only) > 30 else ''}")
    if x_only:
        print(f"   Codex only:  {', '.join(x_only[:30])}{' …' if len(x_only) > 30 else ''}")
    mc, mx = _mcp_servers()
    print(f"\n Connectors (MCP servers): {len(mc & mx)} both · {len(mc - mx)} Claude only · {len(mx - mc)} Codex only")
    if mc - mx:
        print(f"   Claude only: {', '.join(sorted(mc - mx)[:20])}")
    if mx - mc:
        print(f"   Codex only:  {', '.join(sorted(mx - mc)[:20])}")
    print(f"\n Claudex itself: rules {rules_file()}, memory {root / '.claudex'} (shared by every role)")
    if link_skills and (c_only or x_only):
        moves = [(sk[n]["claude"], pathlib.Path.home() / ".codex/skills" / n) for n in c_only]
        moves += [(sk[n]["codex"], pathlib.Path.home() / ".claude/skills" / n) for n in x_only]
        print(f"\nLinking {len(moves)} skill(s) so both agents have them (links, not copies; nothing is deleted).")
        if _ask_yes("Go ahead?", yes):
            for src, dst in moves:
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.symlink_to(src)
            print(f" {G}done{RESET}")
    elif c_only or x_only:
        print(f"\n {D}`claudex shared --link-skills` makes every skill available to both (by linking).{RESET}")
    return 0


# ---------- memory ----------
def memory(cwd=".", files=False):
    root = _root(cwd)
    base = root / ".claudex"
    if not base.exists():
        print("No Claudex memory here yet. Run `claudex init`, or just run a job.")
        return 0
    rows = []
    for f in sorted(base.rglob("*")):
        if f.is_file() and not f.name.endswith(".lock"):
            st = f.stat()
            age = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%d %b %H:%M")
            rows.append((str(f.relative_to(root)), st.st_size, age))
    print(f"{B}Claudex memory for {root.name}{RESET}  ({base})\n")
    shown = rows if files else [r for r in rows if "/handovers/" not in r[0]][:40]
    for name, size, age in shown:
        print(f"  {name:<64} {size:>8,} B  {D}{age}{RESET}")
    if not files and len(shown) < len(rows):
        print(f"  {D}… {len(rows) - len(shown)} more (claudex memory --files){RESET}")
    if files:
        return 0
    for title, f, n in (("Project map", base / "project/map.md", 1800), ("Recent decisions", base / "project/decisions.md", 1200),
                        ("Recent jobs", base / "project/journal.md", 1800)):
        if f.exists():
            text = f.read_text(errors="replace")
            print(f"\n{B}{title}{RESET}\n" + ("…" + text[-n:] if len(text) > n else text).rstrip())
    return 0


# ---------- permissions ----------
def show_permissions(cwd=".", n=30, edit=False):
    root = _root(cwd)
    if edit:
        target = root / ".claudex" / "rules.toml"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(PROJECT_RULES_TEMPLATE)
        print(f"Editing this project's rules ({target}). Your main rules: {USER_RULES if USER_RULES.exists() else rules_file()}")
        return subprocess.call([os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano", str(target)])
    rules = load_rules(project_root=root)
    p = rules.get("permissions", {})
    print(f"{B}Permissions{RESET}  ({root.name})\n")
    for k, (label, _) in MODES.items():
        print(f"  {B}{k:<13}{RESET}{label}")
    print(f"\n  Workers ask before acting; Claudex answers: {R}deny{RESET} rules, then {Y}ask{RESET} rules (you), then "
          f"{G}allow{RESET} rules,\n  then the gatekeeper ({p.get('gatekeeper', {}).get('model', 'none')}). Chained commands "
          "never match an allow rule. With nobody watching, 'ask you' becomes 'no'.\n")
    for k, colour in (("deny", R), ("ask", Y), ("allow", G)):
        items = p.get(k, [])
        print(f"  {colour}{k}{RESET} ({len(items)}): " + ", ".join(items[:14]) + (" …" if len(items) > 14 else ""))
    recent = perms.recent(root / ".claudex" / "permissions.jsonl", n)
    print(f"\n{B}Last {len(recent)} decisions{RESET}" + ("" if recent else ": none yet"))
    for r in recent:
        colour = G if r.get("decision") == "allow" else R
        print(f"  {D}{r['at'][5:16]}{RESET} {colour}{r['decision']:<5}{RESET} {r.get('by', ''):<14} "
              f"{r.get('agent', ''):<6} {r.get('kind', ''):<6} {r.get('what', '')[:60]}  {D}{r.get('why', '')[:50]}{RESET}")
    print(f"\n{D}Change them: `claudex permissions --edit` (this project) or `claudex rules --edit` (everywhere).{RESET}")
    return 0


# ---------- usage ----------
def show_usage(days=14, project=None, web=False, lan=False, port=8765, as_json=False, no_open=False):
    if web:
        usage.serve("0.0.0.0" if lan else "127.0.0.1", port, project, open_browser=not no_open)
        return 0
    s = usage.summary(days, project)
    print(json.dumps(s, indent=1) if as_json else usage.terminal(s, colour=TTY))
    return 0
