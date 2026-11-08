"""Human-owned project preferences, separate from routing rules and worker memory."""
import copy
import json
import os
import pathlib
import tempfile

MODES = ("ask", "auto", "unrestricted")
ROUTES = ("auto", "claude", "codex")
CHECKS = ("full", "reviews", "off")


def path(root):
    return pathlib.Path(root) / ".claudex" / "settings.json"


def load(root):
    target = path(root)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text())
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        for key, choices in (("mode", MODES), ("route", ROUTES), ("checks", CHECKS)):
            if key in data and data[key] not in choices:
                raise ValueError(f"invalid {key}")
        for key in ("base_prompt", "description"):
            if key in data and not isinstance(data[key], str):
                raise ValueError(f"invalid {key}")
        enabled = data.get("enabled_models", {})
        if not isinstance(enabled, dict):
            raise ValueError("invalid model choices")
        for agent, models in enabled.items():
            if agent not in ("claude", "codex") or not isinstance(models, list) or not models or not all(
                    isinstance(m, str) and m.strip() for m in models):
                raise ValueError("choose at least one model for each agent")
        return data
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Cannot read {target}: {exc}") from exc


def save(root, updates):
    """Merge settings atomically; leave invalid existing preferences untouched."""
    from .locks import Lock
    target = path(root)
    lock = Lock(target.with_suffix(".lock"))
    if not lock.try_acquire():
        raise ValueError("Project settings are being saved in another window. Try again.")
    tmp = None
    try:
        data = {**load(root), **updates}
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as f:
            tmp = f.name
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, target)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        lock.release()
    return data


def allowed_model(rules, agent, model):
    enabled = rules.get("_enabled_models", {}).get(agent, [])
    return model if not enabled or model in enabled else enabled[0]


def apply_models(rules, settings):
    enabled = settings.get("enabled_models", {})
    if not enabled:
        return rules
    rules = copy.deepcopy(rules)
    rules["_enabled_models"] = enabled

    def walk(value, agent=None):
        if isinstance(value, dict):
            agent = value.get("agent", agent)
            if agent in enabled and "model" in value:
                value["model"] = allowed_model(rules, agent, value["model"])
            for key, child in value.items():
                walk(child, key if key in ("claude", "codex") else agent)
        elif isinstance(value, list):
            for child in value:
                walk(child, agent)
    walk(rules)
    for agent, models in enabled.items():
        if agent in rules.get("ladder", {}):
            rules["ladder"][agent] = [r for r in rules["ladder"][agent] if r[0] in models]
    return rules


def model_choices(rules, catalog):
    found = {a: list(catalog.get(a, [])) for a in ("claude", "codex")}
    def walk(value, agent=None):
        if isinstance(value, dict):
            agent = value.get("agent", agent)
            if agent in found and value.get("model"):
                found[agent].append(value["model"])
            for key, child in value.items():
                walk(child, key if key in found else agent)
        elif isinstance(value, list):
            for child in value:
                walk(child, agent)
    walk(rules)
    for agent, rungs in rules.get("ladder", {}).items():
        if agent in found:
            found[agent].extend(r[0] for r in rungs)
    return {a: list(dict.fromkeys(m for m in ms if m != "default")) for a, ms in found.items()}


def create_instructions(root, description, prompt):
    """Create only missing files; never rewrite existing project instructions."""
    from .admin import AGENTS_TEMPLATE
    root = pathlib.Path(root)
    contents = {"AGENTS.md": AGENTS_TEMPLATE + "\n## Project brief\n" + description.strip() +
                "\n\n## Working instructions\n" + prompt.strip() + "\n", "CLAUDE.md": "@AGENTS.md\n"}
    created = []
    for name, text in contents.items():
        try:
            with (root / name).open("x") as f:
                f.write(text)
            created.append(name)
        except FileExistsError:
            pass
    return created
