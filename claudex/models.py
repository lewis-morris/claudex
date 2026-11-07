"""models.md: who does what, in a plain Markdown file anyone can edit.

Where Claudex looks (first found wins): `--models FILE`, then the project's `.claudex/models.md`, then your
`~/.config/claudex/models.md`. Without one, the table in rules.toml is used.

What it can hold:
- a table with columns When | Agent | Model | Reasoning: replaces the "who does what" rules
- lines like `Lead: claude opus high`, `Default: codex gpt-6.1-sol medium`, `Claude reviewer: opus high`,
  `Codex reviewer: gpt-6-astra high`, `Gatekeeper: claude sonnet low`
- anything else you write (sentences, bullet points): passed to the lead as your notes on who should do what,
  e.g. "Anything touching the database goes to Codex Astra, high."
"""
import os
import pathlib
import re

from .paths import USER_RULES

USER_MODELS = USER_RULES.with_name("models.md")
AGENTS = ("claude", "codex")
EFFORT = ("low", "medium", "high", "xhigh")
_SETTING = re.compile(r"^\s*[-*]?\s*(lead|default|claude reviewer|codex reviewer|gatekeeper)\s*:\s*(.+)$", re.I)


def find(project_root=None, explicit=None):
    if explicit:
        return pathlib.Path(explicit)
    if project_root:
        own = pathlib.Path(project_root) / ".claudex" / "models.md"
        if own.is_file():
            return own
    return USER_MODELS if USER_MODELS.is_file() else None


def _cells(line):
    return [c.strip().strip("`") for c in line.strip().strip("|").split("|")]


def _spec(words):
    """'claude opus high' / 'opus high' -> (agent or None, model, effort or None)."""
    words = [w.strip(",.").lower() for w in words.split()]
    agent = words.pop(0) if words and words[0] in AGENTS else None
    effort = words.pop() if words and words[-1] in EFFORT else None
    return agent, " ".join(words), effort


def parse(text):
    rows, settings, notes = [], {}, []
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)       # comments are for people, not the lead
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|") and i + 1 < len(lines) and set(lines[i + 1].replace("|", "").strip()) <= set("-: "):
            header = [h.lower() for h in _cells(line)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                c = dict(zip(header, _cells(lines[i])))
                when = c.get("when") or c.get("job") or ""
                agent = (c.get("agent") or c.get("who") or "").lower()
                model = c.get("model") or ""
                effort = (c.get("reasoning") or c.get("effort") or "medium").lower()
                if when and agent in AGENTS and model and effort in EFFORT:
                    use = {"agent": agent, "model": model, "effort": effort}
                    if "browser" in (c.get("browser") or "").lower() or "yes" == (c.get("browser") or "").lower():
                        use["browser"] = True
                    rows.append({"when": when, "use": use})
                i += 1
            continue
        m = _SETTING.match(line)
        if m:
            settings[m.group(1).lower()] = _spec(m.group(2))
        elif line.strip() and not line.lstrip().startswith("#"):
            notes.append(line.rstrip())
        i += 1
    return {"rows": rows, "settings": settings, "notes": "\n".join(notes).strip()}


def apply(rules, path):
    """Rules with a models.md layered on top. Records where it came from for `claudex models`."""
    if not path or not pathlib.Path(path).is_file():
        return rules
    m = parse(pathlib.Path(path).read_text(errors="replace"))
    rules = dict(rules)
    if m["rows"]:
        rules["rule"] = m["rows"]
    s = m["settings"]

    def put(section, key, spec, default_agent):
        agent, model, effort = spec
        cur = dict(rules.get(section, {}) if key is None else rules.get(section, {}).get(key, {}))
        if agent and key is None:
            cur["agent"] = agent
        if model:
            cur["model"] = model
        if effort:
            cur["effort"] = effort
        if key is None:
            rules[section] = cur
        else:
            rules[section] = dict(rules.get(section, {}), **{key: cur})
    if "lead" in s:
        put("lead", None, s["lead"], "claude")
    if "default" in s:
        put("defaults", None, s["default"], "codex")
    if "claude reviewer" in s:
        put("reviewers", "claude", s["claude reviewer"], "claude")
    if "codex reviewer" in s:
        put("reviewers", "codex", s["codex reviewer"], "codex")
    if "gatekeeper" in s:
        perms = dict(rules.get("permissions", {}))
        g = dict(perms.get("gatekeeper", {}))
        agent, model, effort = s["gatekeeper"]
        g.update({k: v for k, v in (("agent", agent), ("model", model), ("effort", effort)) if v})
        perms["gatekeeper"] = g
        rules["permissions"] = perms
    rules["_models_file"] = str(path)
    rules["_lead_notes"] = m["notes"]
    return rules


def render(rules):
    """A models.md that reproduces the rules in force, ready to edit."""
    lead, d, rv = rules["lead"], rules["defaults"], rules.get("reviewers", {})
    g = rules.get("permissions", {}).get("gatekeeper", {})
    out = ["# Who does what", "",
           "<!-- Claudex reads this file before planning. Edit the table and the settings under it, and write any",
           "     notes for the lead under 'Notes for the lead': everything outside comments that isn't the table or",
           "     a setting is passed to the lead as your guidance. Agents: claude or codex. Reasoning: low, medium,",
           "     high or xhigh. Browser: yes for jobs that need your signed-in Chrome (Claude only). -->", "",
           "| When | Agent | Model | Reasoning | Browser |", "|---|---|---|---|---|"]
    for r in rules.get("rule", []):
        u = r["use"]
        out.append(f"| {r['when']} | {u['agent']} | {u['model']} | {u['effort']} | {'yes' if u.get('browser') else ''} |")
    out += ["", f"Lead: {lead.get('agent', 'claude')} {lead['model']} {lead['effort']}",
            f"Default: {d['agent']} {d['model']} {d['effort']}"]
    if rv.get("claude"):
        out.append(f"Claude reviewer: {rv['claude']['model']} {rv['claude']['effort']}")
    if rv.get("codex"):
        out.append(f"Codex reviewer: {rv['codex']['model']} {rv['codex']['effort']}")
    if g:
        out.append(f"Gatekeeper: {g.get('agent', 'claude')} {g.get('model', 'sonnet')} {g.get('effort', 'low')}")
    out += ["", "## Notes for the lead", "",
            "<!-- For example: - Anything touching the database goes to Codex Astra, high. -->", ""]
    return "\n".join(out)
