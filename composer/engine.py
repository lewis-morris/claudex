"""The composer: a lead plans each message, then one worker at a time does each step."""
import asyncio
import datetime
import json
import os
import pathlib
import subprocess
import tomllib

from .child import Child

HOME = pathlib.Path(__file__).resolve().parent.parent
RULES = pathlib.Path(os.environ.get("COMPOSER_RULES", HOME / "rules.toml"))
CATALOG = pathlib.Path.home() / ".config/agent-of-empires/acp_option_catalog.json"
NOTES_LIMIT = 8000

ROUTES = {"auto": "Auto (composer decides)", "claude": "Claude only", "codex": "Codex only"}
EFFORTS = ["auto", "low", "medium", "high", "xhigh"]
# Composer mode -> each worker's own permission mode.
MODES = {
    "default": ("Ask first", {"claude": "default", "codex": "read-only"}),
    "acceptEdits": ("Accept edits", {"claude": "acceptEdits", "codex": "workspace-write"}),
    "auto": ("Auto", {"claude": "auto", "codex": "agent"}),
    "bypassPermissions": ("Bypass permissions", {"claude": "bypassPermissions", "codex": "agent-full-access"}),
}

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["steps"],
    "properties": {"steps": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "agent", "model", "effort", "browser", "why", "instructions"],
        "properties": {
            "title": {"type": "string"}, "agent": {"enum": ["claude", "codex"]},
            "model": {"type": "string"}, "effort": {"enum": ["low", "medium", "high", "xhigh"]},
            "browser": {"type": "boolean"}, "why": {"type": "string"},
            "instructions": {"type": "string"}}}}}}


def load_rules():
    return tomllib.loads(RULES.read_text())


def catalog_models():
    """Models each agent offered when Agent of Empires last asked (fallback before a worker starts)."""
    try:
        agents = json.loads(CATALOG.read_text())["agents"]
    except Exception:
        return {}
    out = {}
    for name in ("claude", "codex"):
        for opt in agents.get(name, {}).get("options", []):
            if opt.get("id") == "model":
                out[name] = [o["value"] for o in opt["options"] if o["value"] != "default"]
    return out


class Engine:
    def __init__(self, cwd, ui, mcp_servers=None):
        self.cwd = str(cwd)
        self.ui = ui
        self.mcp_servers = mcp_servers or []
        self.route, self.effort, self.mode = "auto", "auto", "auto"
        self.children = {}
        self.active = None
        self.cancelled = False
        self.notes_dir = pathlib.Path(self.cwd) / ".composer"
        self.notes = self.notes_dir / "notes.md"
        self.log = self.notes_dir / "steps.jsonl"

    # ---------- shared memory ----------
    def _prepare_notes(self):
        self.notes_dir.mkdir(exist_ok=True)
        exclude = pathlib.Path(self.cwd) / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():  # keep notes out of git without touching tracked files
            text = exclude.read_text() if exclude.exists() else ""
            if ".composer/" not in text:
                exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".composer/\n")

    def _notes_tail(self):
        if not self.notes.exists():
            return "(none yet)"
        t = self.notes.read_text()
        return t if len(t) <= NOTES_LIMIT else "…(earlier notes trimmed)…\n" + t[-NOTES_LIMIT:]

    def _append_notes(self, text):
        with self.notes.open("a") as f:
            f.write(text)

    # ---------- workers ----------
    async def child(self, name, rules):
        c = self.children.get(name)
        if c and c.proc and c.proc.returncode is None:
            return c
        c = Child(name, rules["agents"][name], self.cwd, self.ui, self.mcp_servers)
        await c.start()
        self.children[name] = c
        return c

    async def close(self):
        for c in self.children.values():
            await c.close()

    def cancel(self):
        self.cancelled = True
        if self.active:
            self.active.cancel()

    # ---------- planning ----------
    async def plan(self, text, rules):
        forced = self.route if self.route != "auto" else None
        for tag in ("claude", "codex"):
            if text.lstrip().lower().startswith("@" + tag):
                forced, text = tag, text.lstrip()[len(tag) + 1:].lstrip()
        if forced:
            d = rules["agent_defaults"][forced]
            return text, [{"title": "Your request", "agent": forced, "model": d["model"], "effort": d["effort"],
                           "browser": False, "why": f"you chose {forced.title()} only", "instructions": text}]
        models = catalog_models()
        for name, c in self.children.items():
            if c.options.get("model"):
                models[name] = [m for m in c.options["model"] if m != "default"]
        try:
            files = subprocess.run(["git", "ls-files"], cwd=self.cwd, capture_output=True, text=True,
                                   timeout=10).stdout.splitlines()
        except Exception:
            files = []
        prompt = (
            "You are the lead of a two-agent coding team: Claude Code and OpenAI Codex. Split the user's "
            "message into the FEWEST sequential steps (usually one) and give each step to the best agent, "
            "model and reasoning level. Only one agent works at a time; every step sees the shared notes. "
            "Do not split a job just because both agents could help; split only when parts clearly suit "
            "different agents. Don't add a separate testing step: each worker tests its own work.\n\n"
            f"USER MESSAGE:\n{text}\n\n"
            f"PROJECT FILES ({len(files)}):\n" + "\n".join(files[:200]) + "\n\n"
            f"SHARED NOTES SO FAR:\n{self._notes_tail()}\n\n"
            f"ROUTING RULES (prefer these; explain any deviation in 'why'):\n{json.dumps(rules['rule'], indent=1)}\n"
            f"DEFAULT when no rule clearly fits: {json.dumps(rules['defaults'])}\n"
            f"MODELS EACH AGENT OFFERS NOW: {json.dumps(models)}\n"
            f"Only {rules['browser']['signed_in']} can use the user's signed-in browser.\n"
            "Write each step's instructions so the worker can act without seeing this message.")
        lead = rules["lead"]
        cmd = ["claude", "-p", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--disable-slash-commands", "--setting-sources", "project", "--tools", "",
               "--model", lead["model"], "--effort", lead["effort"], "--max-budget-usd", "0.5",
               "--output-format", "json", "--json-schema", json.dumps(PLAN_SCHEMA)]
        try:
            p = await asyncio.create_subprocess_exec(*cmd, cwd=self.cwd, stdin=asyncio.subprocess.PIPE,
                                                     stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(prompt.encode()), 180)
            steps = json.loads(out)["structured_output"]["steps"]
        except Exception as e:
            d = rules["defaults"]
            await self.ui.say(f"_(The lead couldn't plan this ({type(e).__name__}); using the default worker.)_\n\n")
            steps = [{"title": "Your request", "agent": d["agent"], "model": d["model"], "effort": d["effort"],
                      "browser": False, "why": "default", "instructions": text}]
        return text, steps

    # ---------- one message ----------
    async def turn(self, text):
        self.cancelled = False
        rules = load_rules()
        self._prepare_notes()
        await self.ui.say("_Planning…_\n\n")
        text, steps = await self.plan(text, rules)
        if self.cancelled:
            return "cancelled"
        for s in steps:  # a step needing the signed-in browser must go to an agent that has it
            if s["browser"] and s["agent"] not in rules["browser"]["signed_in"]:
                s["agent"], s["why"] = rules["browser"]["signed_in"][0], s["why"] + " (moved: needs the signed-in browser)"
                d = rules["agent_defaults"][s["agent"]]
                s["model"] = d["model"]
            if self.effort != "auto":
                s["effort"] = self.effort
        entries = [{"content": f"{s['title']} · {s['agent'].title()} {s['model']} {s['effort']}",
                    "priority": "medium", "status": "pending"} for s in steps]
        await self.ui.plan(entries)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self._append_notes(f"\n# {stamp} · Request\n{text}\n\n")

        for i, s in enumerate(steps, 1):
            if self.cancelled:
                return "cancelled"
            entries[i - 1]["status"] = "in_progress"
            await self.ui.plan(entries)
            header = f"**Step {i}/{len(steps)} · {s['agent'].title()} · {s['model']} · {s['effort']}**"
            await self.ui.say(f"{header}  \n_{s['title']}: {s['why']}_\n\n")
            try:
                c = await self.child(s["agent"], rules)
            except Exception as e:
                await self.ui.say(f"Couldn't start {s['agent']}: {e}\n\n")
                entries[i - 1]["status"] = "completed"
                await self.ui.plan(entries)
                continue
            self.active = c
            if not await c.set("model", s["model"]):
                fallback = rules["agent_defaults"][s["agent"]]["model"]
                await c.set("model", fallback)
                await self.ui.say(f"_({s['model']} isn't offered right now; using {fallback}.)_\n\n")
            await c.set(rules["effort_option"][s["agent"]], s["effort"])
            await c.set("mode", MODES[self.mode][1][s["agent"]])
            brief = (
                f"You are one worker in Composer, a team where Claude Code and Codex take turns on a job.\n"
                f"Step {i} of {len(steps)}: {s['title']}\n\n{s['instructions']}\n\n"
                f"The user's original message:\n{text}\n\n"
                f"Shared notes (earlier work by you or the other agent):\n{self._notes_tail()}\n\n"
                "Do only this step. Check your own work where you can (run the tests). "
                "Do not commit unless asked. If you changed files, end with a short handoff: what you "
                "changed (files), what you checked and the result, and anything the next agent must know. "
                "If you only answered a question, just answer it.")
            started = asyncio.get_running_loop().time()
            try:
                stop, out = await c.prompt(brief)
            except Exception as e:
                stop, out = "error", f"(worker failed: {e})"
                await self.ui.say(f"\n\n{s['agent'].title()} stopped with an error: {e}\n\n")
            finally:
                self.active = None
            secs = round(asyncio.get_running_loop().time() - started, 1)
            self._append_notes(f"## Step {i}: {s['title']} ({s['agent']} {s['model']} {s['effort']})\n{out}\n\n")
            with self.log.open("a") as f:
                f.write(json.dumps({"at": stamp, "step": i, "title": s["title"], "agent": s["agent"],
                                    "model": s["model"], "effort": s["effort"], "secs": secs,
                                    "stop": stop}) + "\n")
            entries[i - 1]["status"] = "completed"
            await self.ui.plan(entries)
            await self.ui.say("\n\n")
            if stop == "cancelled" or self.cancelled:
                return "cancelled"
        return "end_turn"
