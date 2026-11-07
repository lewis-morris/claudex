"""Finding things on whoever's computer this runs on: Node tools, the rules file, the Chrome launcher."""
import glob
import os
import pathlib
import shlex
import shutil

PACKAGE = pathlib.Path(__file__).resolve().parent
DEFAULT_RULES = PACKAGE / "rules.toml"
USER_RULES = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "claudex/rules.toml"
CACHE = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "claudex"


def rules_file():
    """Which rules are in force: $CLAUDEX_RULES, else your own copy, else the defaults that ship with Claudex."""
    env = os.environ.get("CLAUDEX_RULES") or os.environ.get("COMPOSER_RULES")
    if env:
        return pathlib.Path(env)
    return USER_RULES if USER_RULES.is_file() else DEFAULT_RULES


def _version_key(path):
    parts = pathlib.Path(path).parent.name.lstrip("v").split(".")
    return [int(p) if p.isdigit() else 0 for p in parts]


def tool_dirs():
    """Where `claude`, `codex` and the ACP connectors usually live, best first."""
    dirs = []
    node = shutil.which("node")
    if node:
        dirs.append(str(pathlib.Path(node).parent))
    dirs += sorted(glob.glob(str(pathlib.Path.home() / ".nvm/versions/node/*/bin")), key=_version_key, reverse=True)
    dirs += [str(pathlib.Path.home() / p) for p in (".local/bin", ".npm-global/bin", ".bun/bin", ".volta/bin")]
    dirs += ["/opt/homebrew/bin", "/usr/local/bin"]
    seen, out = set(), []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def tool_env(extra=None):
    """Your PATH comes first (so the versions you chose win); common install folders are only fallbacks."""
    env = dict(os.environ)
    mine = [d for d in env.get("PATH", "").split(os.pathsep) if d]
    first = [os.environ["CLAUDEX_NODE_BIN"]] if os.environ.get("CLAUDEX_NODE_BIN") else []
    fallback = [d for d in tool_dirs() if d not in mine and d not in first]
    env["PATH"] = os.pathsep.join(first + mine + fallback)
    env.update(extra or {})
    return env


def which(name):
    if os.path.isabs(name):
        return name if os.access(name, os.X_OK) else None
    return shutil.which(name, path=tool_env()["PATH"])


def chrome_launcher():
    """A tiny launcher that starts Claude with Claude in Chrome switched on. The ACP connector starts Claude
    without --chrome, so the browser worker points CLAUDE_CODE_EXECUTABLE here. Made on first use."""
    claude = None
    acp = which("claude-agent-acp")
    if acp:   # prefer the Claude bundled with the connector: it's the one the connector expects
        pkg = pathlib.Path(os.path.realpath(acp)).parent.parent
        found = sorted(glob.glob(str(pkg / "node_modules/@anthropic-ai/claude-agent-sdk-*/claude")))
        claude = found[0] if found else None
    claude = claude or which("claude")
    if not claude:
        return None
    path = CACHE / "claude-chrome"
    text = f'#!/bin/sh\n# Made by Claudex: Claude with Claude in Chrome switched on.\nexec {shlex.quote(claude)} --chrome "$@"\n'
    if not path.exists() or path.read_text() != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
    return str(path)


def self_command(*args, rules=None):
    """(argv, env) to run another Claudex command (doctor, rules, log) as its own process, whether Claudex
    was installed or is running from a checkout."""
    import sys
    env = dict(os.environ)
    if rules:
        env["CLAUDEX_RULES"] = os.path.abspath(rules)
    # -I: ignore the current folder and PYTHONPATH, so a project with its own "claudex" folder can't stand in;
    # then load this very copy of Claudex explicitly.
    boot = ("import sys; sys.path.insert(0, sys.argv.pop(1)); sys.argv[0] = 'claudex'; "
            "from claudex.__main__ import main; main()")
    return [sys.executable, "-I", "-c", boot, str(PACKAGE.parent), *args], env
