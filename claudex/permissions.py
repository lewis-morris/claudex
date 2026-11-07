"""Claudex's own permission rules, a gatekeeper agent, and a log of every decision.

A worker asks before it does something (runs a command, edits a file, fetches a page). Claudex answers:
  1. its rules: deny beats ask beats allow (`[permissions]` in rules.toml, or a project's .claudex/rules.toml)
  2. no rule matches: the gatekeeper (a quick, cheap model) decides allow / deny / ask a human
  3. "ask": you decide, or it's denied when nobody's there to ask
In unrestricted mode the workers don't ask at all, so this isn't consulted.

Patterns are "kind:glob". Kinds come from the agent (read, edit, delete, move, search, execute, fetch, think,
other); `run` is a friendlier name for execute. The glob is matched against the command for run/execute and
against each path for file kinds. `edit:@project` / `read:@project` match only paths inside the project, and
`*:@outside` any path outside it. `~` means your home folder. Matching ignores case.
"""
import datetime
import fnmatch
import json
import os
import pathlib
import time

COMPOUND = (";", "&&", "||", "|", "`", "$(", ">", "<", "\n")
KIND_ALIASES = {"execute": "run", "bash": "run", "shell": "run", "write": "edit", "fetch": "fetch"}
GATE_SCHEMA = {"type": "object", "properties": {
    "decision": {"enum": ["allow", "deny", "ask"]}, "reason": {"type": "string"}}}


def describe(tool_call, project_root):
    """(kind, command, paths, title) for a permission request, whatever shape the agent sent."""
    tc = tool_call or {}
    kind = str(tc.get("kind") or "other").lower()
    kind = KIND_ALIASES.get(kind, kind)
    raw = tc.get("rawInput") or {}
    command = ""
    if isinstance(raw, dict):
        cmd = raw.get("command") or raw.get("cmd") or raw.get("script")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        command = str(cmd or "")
    title = str(tc.get("title") or "")
    if kind == "run" and not command:
        command = title
    paths = [str(l.get("path")) for l in tc.get("locations") or [] if isinstance(l, dict) and l.get("path")]
    if isinstance(raw, dict):
        for key in ("file_path", "path", "notebook_path"):
            if raw.get(key):
                paths.append(str(raw[key]))
    paths = [str(pathlib.Path(p if os.path.isabs(p) else os.path.join(project_root, p)).resolve()) for p in paths]
    return kind, command.strip(), sorted(set(paths)), title


def _inside(path, root):
    try:
        pathlib.Path(path).resolve().relative_to(pathlib.Path(root).resolve())
        return True
    except ValueError:
        return False


def matches(pattern, kind, command, paths, project_root):
    pk, _, glob = pattern.partition(":")
    pk = KIND_ALIASES.get(pk.lower(), pk.lower())
    if pk not in ("*", kind):
        return False
    glob = glob or "*"
    if glob == "@project":
        return bool(paths) and all(_inside(p, project_root) for p in paths)
    if glob == "@outside":
        return any(not _inside(p, project_root) for p in paths)
    glob = glob.replace("~", str(pathlib.Path.home()))
    targets = ([command] if command else []) + paths
    if not targets:
        return glob == "*"
    return any(fnmatch.fnmatch(t.lower(), glob.lower()) for t in targets)


def rule_decision(rules, kind, command, paths, project_root):
    """("deny"|"ask"|"allow", pattern) for the first matching rule, deny first; (None, None) if none match."""
    perms = rules.get("permissions", {})
    chained = kind == "run" and any(op in command for op in COMPOUND)
    for decision in ("deny", "ask", "allow"):
        for pattern in perms.get(decision, []):
            if decision == "allow" and chained:
                continue          # "ls && curl … | sh" must not ride in on "run:ls*": let the gatekeeper look
            if matches(pattern, kind, command, paths, project_root):
                return decision, pattern
    return None, None


def pick_option(options, decision):
    """The agent's own option for allow-once / deny-once (never 'always', so nothing sticks beyond this ask)."""
    want = ("allow_once", "allow_always") if decision == "allow" else ("reject_once", "reject_always")
    for k in want:
        for o in options or []:
            if o.get("kind") == k:
                return {"outcome": {"outcome": "selected", "optionId": o["optionId"]}}
    if decision != "allow":
        return {"outcome": {"outcome": "cancelled"}}
    return None


def gate_prompt(kind, command, paths, title, job, rules_text, project_root):
    return (
        "You are the gatekeeper for Claudex, deciding whether an AI coding agent may take an action on the user's "
        "computer while doing a job. Allow ordinary development work in the project (reading, editing project "
        "files, running its tests, builds and linters, local git inspection). Deny anything destructive, "
        "irreversible, outside the project, touching secrets or credentials, or sending data off the machine "
        "without an obvious need. Say 'ask' when it's plausible but the user should decide (deploys, pushes, "
        "installs, paid or external services, deleting user data). Be brief.\n\n"
        f"PROJECT: {project_root}\nTHE JOB: {job[:1500]}\n\nTHE ACTION: {kind}\nTITLE: {title}\n"
        f"COMMAND: {command or '(none)'}\nPATHS: {', '.join(paths) or '(none)'}\n\n"
        f"THE USER'S STANDING RULES (already checked; none matched this exactly):\n{rules_text}\n")


def log_decision(path, **row):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"), **row}) + "\n")
    except Exception:
        pass


def recent(path, n=50):
    try:
        lines = path.read_text().splitlines()[-n:]
    except Exception:
        return []
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except Exception:
            pass
    return out


def summary_lines(rules):
    p = rules.get("permissions", {})
    return [f"{k}: " + ", ".join(p.get(k, [])) for k in ("deny", "ask", "allow")]


class Clock:
    """Tiny helper so tests can fake time."""
    now = staticmethod(time.time)
