"""Slash commands and skills: one list shared by the terminal menu and Agents of Empires' `/` menu."""
import pathlib
import re

SKILL_DIRS = {"claude": pathlib.Path.home() / ".claude/skills", "codex": pathlib.Path.home() / ".codex/skills"}

# (name, argument hint, what it does). Settings commands only make sense in the terminal; in Agents of
# Empires the same settings are the Routing / Effort / Mode / Checks pickers.
COMMANDS = [
    ("help", "", "show every command and skill"),
    ("auto", "", "Claudex decides who does each part (default)"),
    ("claude", "", "send everything to Claude"),
    ("codex", "", "send everything to Codex"),
    ("effort", "auto|low|medium|high|xhigh", "force one reasoning level for every step"),
    ("mode", "ask|auto|unrestricted", "ask: you approve everything · auto: rules + gatekeeper · unrestricted"),
    ("checks", "full|reviews|off", "second opinions + reviews / reviews only / none"),
    ("dry", "", "plan only: show who'd do what, change nothing (toggle)"),
    ("anim", "playful|calm|off big|small|tiny", "how lively and how big the monkeys are (F2 steps through all nine)"),
    ("status", "", "current settings and where the notes are"),
    ("models", "", "who does what (models.md), and where that comes from"),
    ("rules", "", "the full rules file"),
    ("doctor", "", "check logins, connectors and models"),
    ("log", "", "recent steps in this project"),
    ("memory", "", "this project's memory: files, map, decisions, recent jobs"),
    ("shared", "", "what Claude and Codex share (house rules, skills, connectors) and what they don't"),
    ("permissions", "", "the permission rules in force and every recent decision"),
    ("init", "", "set this project up: one house-rules file for both agents, memory, first map"),
    ("usage", "web", "tokens across Claude and Codex as a coloured chart; /usage web opens a live page"),
    ("quit", "", "leave Claudex"),
]
TERMINAL_ONLY = {"auto", "claude", "codex", "effort", "mode", "checks", "dry", "quit", "anim"}


def _describe(skill_md):
    try:
        text = skill_md.read_text(errors="replace")
    except Exception:
        return ""
    m = re.search(r"^description:\s*(.+)$", text, re.M)
    if not m:
        return ""
    d = m.group(1).strip().strip("\"'")
    first = re.split(r"(?<=[.!?])\s|—", d, maxsplit=1)[0].strip()
    return first if len(first) <= 90 else first[:87].rstrip() + "…"


def load_skills():
    """{name: {"description": str, "agents": ["claude", "codex"]}} for every installed skill."""
    out = {}
    for agent, root in SKILL_DIRS.items():
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            md = d / "SKILL.md"
            if d.name.endswith("-workspace") or not md.is_file():
                continue
            entry = out.setdefault(d.name, {"description": _describe(md), "agents": []})
            entry["agents"].append(agent)
    return out


def expand_skill(text, skills):
    """'/deployify ship it' -> ('Use the deployify skill. ship it', 'claude') when it's a skill, else None.

    A skill only one agent has goes to that agent; one both have is left to the lead.
    """
    m = re.match(r"^/([\w.-]+)(?:\s+(.*))?$", text.strip(), re.S)  # "/skill" or "/skill words", not "/skill/path"
    if not m or m.group(1) not in skills:
        return None
    name, rest = m.group(1), (m.group(2) or "").strip()
    agents = skills[name]["agents"]
    forced = agents[0] if len(agents) == 1 else None
    message = f"Use the {name} skill." + (f" {rest}" if rest else "")
    return message, forced


def acp_commands(skills):
    """The `available_commands_update` list for Agents of Empires."""
    cmds = [{"name": n, "description": d, **({"input": {"hint": h}} if h else {})}
            for n, h, d in COMMANDS if n not in TERMINAL_ONLY]
    for name, s in sorted(skills.items()):
        who = " / ".join(a.title() for a in s["agents"])
        cmds.append({"name": name, "description": f"[{who} skill] {s['description']}",
                     "input": {"hint": "what you want done"}})
    return cmds
