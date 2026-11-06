"""Claudex: one lead plans each job, big decisions get a second opinion, one worker at a time does each
part, the opposite side reviews every change, and everything lands in shared project memory."""
import asyncio
import datetime
import json
import os
import pathlib
import subprocess
import time
import tomllib

from . import fun, gitsnap, headless
from .child import Child
from .ladder import next_rung
from .locks import CHROME_LOCK, Lock

HOME = pathlib.Path(__file__).resolve().parent.parent
RULES = pathlib.Path(os.environ.get("CLAUDEX_RULES") or os.environ.get("COMPOSER_RULES") or HOME / "rules.toml")
CATALOG = pathlib.Path.home() / ".config/agent-of-empires/acp_option_catalog.json"
NOTES_LIMIT = 8000
MEMORY_LIMIT = 6000
JOURNAL_LIMIT = 5000
INSTRUCTIONS_LIMIT = 40000   # per file; house rules are never cut silently
HANDOFF_LIMIT = 1500         # each worker's handover kept for the rest of the job and in the journal
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")  # Claude reads one, Codex the other; Claudex shows both to everyone
DECISION_TAG = "DECISION NEEDED:"
START_TIMEOUT = 120  # seconds for a worker to start, or to accept its settings
SNAPSHOT_NOTE = ("(File lists and diffs come from Claudex's own snapshots of the working folder. Nothing has "
                 "been staged or committed in the user's git.)")

ROUTES = {"auto": "Auto (Claudex decides)", "claude": "Claude only", "codex": "Codex only"}
EFFORTS = ["auto", "low", "medium", "high", "xhigh"]
CHECKS = {"full": "Second opinions + reviews", "reviews": "Reviews only", "off": "No checks"}
# Claudex mode -> each worker's own permission mode.
MODES = {
    "default": ("Ask first", {"claude": "default", "codex": "read-only"}),
    "acceptEdits": ("Accept edits", {"claude": "acceptEdits", "codex": "workspace-write"}),
    "auto": ("Auto", {"claude": "auto", "codex": "agent"}),
    "bypassPermissions": ("Bypass permissions", {"claude": "bypassPermissions", "codex": "agent-full-access"}),
}

_STEP = {"type": "object", "properties": {
    "title": {"type": "string"}, "agent": {"enum": ["claude", "codex"]},
    "model": {"type": "string"}, "effort": {"enum": ["low", "medium", "high", "xhigh"]},
    "browser": {"type": "boolean"}, "why": {"type": "string"}, "instructions": {"type": "string"}}}
PLAN_SCHEMA = headless.strict({"type": "object", "properties": {
    "steps": {"type": "array", "minItems": 1, "maxItems": 6, "items": _STEP},
    "big_decisions": {"type": "array", "items": {"type": "object", "properties": {
        "decision": {"type": "string"}, "why": {"type": "string"}, "alternatives": {"type": "string"}}}},
    "concern_responses": {"type": "array", "items": {"type": "object", "properties": {
        "concern": {"type": "string"}, "accepted": {"type": "boolean"}, "why": {"type": "string"}}}}}})
OPINION_SCHEMA = {"type": "object", "properties": {
    "verdict": {"enum": ["agree", "concerns"]}, "concerns": {"type": "array", "items": {"type": "string"}},
    "suggestion": {"type": "string"}}}
REVIEW_SCHEMA = {"type": "object", "properties": {
    "verdict": {"enum": ["pass", "fix"]}, "issues": {"type": "array", "items": {"type": "string"}},
    "summary": {"type": "string"}}}
FINAL_SCHEMA = {"type": "object", "properties": {
    "verdict": {"enum": ["pass", "problems"]}, "problems": {"type": "array", "items": {"type": "string"}},
    "summary": {"type": "string"}}}
MAP_SCHEMA = {"type": "object", "properties": {"map": {"type": "string"}}}


def load_rules(path=None):
    return tomllib.loads(pathlib.Path(path or RULES).read_text())


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


def other(agent):
    return "codex" if agent.startswith("claude") else "claude"


def base(agent):
    return "claude" if agent.startswith("claude") else agent


def tail(text, limit):
    return text if len(text) <= limit else "…(earlier text trimmed)…\n" + text[-limit:]


def repo_root(cwd):
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True,
                             timeout=10)
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


class StepFailed(Exception):
    pass


class Engine:
    def __init__(self, cwd, ui, mcp_servers=None, settings=None, session_key=None):
        s = settings or {}
        self.cwd = str(cwd)
        self.ui = ui
        self.mcp_servers = mcp_servers or []
        self.rules_path = s.get("rules")
        self.route = s.get("route", "auto")
        self.effort = s.get("effort", "auto")
        self.mode = s.get("mode", "auto")
        self.checks = s.get("checks", "full")
        self.dry_run = s.get("dry_run", False)
        self.lead_model, self.lead_effort = s.get("lead_model"), s.get("lead_effort")
        self.children = {}
        self.active = None
        self.cancelled = False
        self.ok = True
        self.busy = False
        self.local = None
        self._task = None
        aoe_dir = os.environ.get("AOE_ARTIFACT_DIR")
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        key = session_key or (pathlib.Path(aoe_dir).name if aoe_dir else f"terminal-{stamp}-{os.getpid()}")
        # One brain per repository: memory and the work lock live at the repo root, wherever Claudex is
        # started inside it. Workers still run in the folder you started in.
        self.project_root = repo_root(self.cwd) or self.cwd
        self.root = pathlib.Path(self.project_root) / ".claudex"
        self.project = self.root / "project"
        self.notes_dir = self.root / "sessions" / key
        self.notes = self.notes_dir / "notes.md"
        self.log = self.notes_dir / "steps.jsonl"
        self.map = self.project / "map.md"
        self.decisions = self.project / "decisions.md"
        self.journal = self.project / "journal.md"   # every finished job, from every session
        self.session_key = key
        self._job = None

    # ---------- settings ----------
    @property
    def reviews_on(self):
        return self.checks in ("full", "reviews")

    @property
    def second_on(self):
        return self.checks == "full"

    # ---------- shared memory ----------
    def _prepare_memory(self):
        if self.dry_run:  # a dry run leaves no trace in the project
            return
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.project.mkdir(parents=True, exist_ok=True)
        # Keep memory out of git without touching tracked files. `--git-path` works in worktrees too.
        try:
            rel = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=self.cwd,
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            rel = ""
        if rel:
            exclude = pathlib.Path(self.cwd) / rel
            exclude.parent.mkdir(parents=True, exist_ok=True)
            text = exclude.read_text() if exclude.exists() else ""
            if ".claudex/" not in text.split():
                exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".claudex/\n")

    def _read(self, path, limit):
        return tail(path.read_text(), limit) if path.exists() else "(none yet)"

    def _append(self, path, text):
        if self.dry_run:
            return
        lock = Lock(self.root / "memory.lock")
        while not lock.try_acquire():
            time.sleep(0.05)
        try:
            with path.open("a") as f:
                f.write(text)
        finally:
            lock.release()

    def _log(self, **row):
        if self._job is not None:
            self._job["rows"].append(row)
        self._append(self.log, json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"),
                                           **row}) + "\n")

    def _act(self, who, state, target=None):
        """Tell a screen that wants to know (the full-screen UI) who is doing what right now."""
        f = getattr(self.ui, "activity", None)
        if f:
            try:
                f(who, state, target)
            except Exception:
                pass

    def _instructions(self):
        """The project's own agent instructions. Claude only reads CLAUDE.md and Codex only AGENTS.md, so both
        are shown to everyone: one set of house rules for the whole team."""
        parts, seen = [], set()
        for folder in dict.fromkeys([self.project_root, self.cwd]):
            for name in INSTRUCTION_FILES:
                f = pathlib.Path(folder) / name
                if not f.is_file() or f.resolve() in seen:
                    continue
                seen.add(f.resolve())
                text = f.read_text(errors="replace")
                if len(text) > INSTRUCTIONS_LIMIT:   # never drop rules silently: say so, and where the rest is
                    text = (text[:INSTRUCTIONS_LIMIT] + f"\n[… {f} continues. Read the whole file before you start; "
                            "its rules all apply.]")
                parts.append(f"--- {f} ---\n{text}")
        return "\n".join(parts) or "(none)"

    def _memory_block(self):
        """The one shared memory. Every role (lead, second opinion, workers, reviewers, final check) gets this."""
        return (f"PROJECT INSTRUCTIONS (CLAUDE.md and AGENTS.md; follow both):\n{self._instructions()}\n\n"
                f"PROJECT MAP:\n{self._read(self.map, MEMORY_LIMIT)}\n\n"
                f"PAST DECISIONS:\n{self._read(self.decisions, MEMORY_LIMIT)}\n\n"
                f"RECENT JOBS IN THIS PROJECT (all sessions):\n{self._read(self.journal, JOURNAL_LIMIT)}\n\n"
                f"THIS JOB SO FAR (every handover and check, never trimmed):\n{self._job_so_far()}\n\n"
                f"THIS SESSION'S EARLIER NOTES:\n{self._read(self.notes, NOTES_LIMIT)}")

    def _job_so_far(self):
        if not self._job or not self._job["rows"]:
            return "(nothing yet)"
        return "\n".join(self._job_lines(self._job["rows"])) or "(nothing yet)"

    @staticmethod
    def _job_lines(rows):
        out = []
        for r in rows:
            if r.get("kind") == "step":
                files = f" (files: {', '.join(r['files'][:8])})" if r.get("files") else ""
                out.append(f"- step {r['step']} {r['title']}: {r['agent']} {r['model']} {r['effort']}, "
                           f"try {r['attempt']} → {r.get('verdict', r.get('stop'))}{files}")
                if r.get("handoff"):
                    out.append("  handover: " + r["handoff"].replace("\n", "\n  "))
                for issue in r.get("issues") or []:
                    out.append(f"  review asked for: {issue}")
            elif r.get("kind") == "second_opinion":
                out.append(f"- second opinion: {r.get('verdict')}"
                           + "".join(f"\n  concern: {c}" for c in r.get("concerns") or []))
            elif r.get("kind") == "final_check":
                out.append(f"- final check: {r.get('verdict')}"
                           + "".join(f"\n  still wrong: {x}" for x in r.get("problems") or []))
        return out

    def _write_journal(self, outcome):
        """One entry per job in the project-wide journal, so other sessions (and agents) know what happened."""
        job = self._job
        if not job or self.dry_run or job.get("written"):
            return
        lines = [f"\n## {job['at']} · {outcome} · session {self.session_key}", f"Request: {job['request'][:300]}"]
        lines += self._job_lines(job["rows"])
        self._append(self.journal, "\n".join(lines) + "\n")
        job["written"] = True   # only after it's safely on disk

    # ---------- workers ----------
    async def child(self, name, rules):
        c = self.children.get(name)
        if c and c.proc and c.proc.returncode is None:
            return c
        c = Child(name, rules["agents"][name], self.cwd, self.ui, self.mcp_servers)
        try:
            await asyncio.wait_for(c.start(), START_TIMEOUT)
        except BaseException:
            await c.close(grace=0)
            raise
        self.children[name] = c
        return c

    async def _stop_child(self, name):
        c = self.children.pop(name, None)
        if c:
            await c.close()

    async def close(self):
        for c in self.children.values():
            await c.close()

    def _clear_our_cancel(self):
        """A stop we asked for is handled here; don't let it leak out as the whole task being cancelled."""
        task = asyncio.current_task()
        if self.cancelled and task is not None and task is self._task:
            while task.cancelling():
                task.uncancel()

    def cancel(self):
        """Stop the job. A worker mid-prompt is asked to stop (and is then waited for); anything else
        (planning, a review, waiting for a lock) is interrupted straight away."""
        self.cancelled = True
        if self.active:
            self.active.cancel()
        elif self._task and not self._task.done():
            self._task.cancel()

    # ---------- the lead ----------
    def _lead(self, rules):
        lead = rules["lead"]
        return (lead.get("agent", "claude"), self.lead_model or lead["model"], self.lead_effort or lead["effort"])

    def _files(self):
        try:
            return subprocess.run(["git", "ls-files"], cwd=self.cwd, capture_output=True, text=True,
                                  timeout=10).stdout.splitlines()
        except Exception:
            return []

    async def plan(self, text, rules, critique=None):
        models = catalog_models()
        for name, c in self.children.items():
            if c.options.get("model"):
                models[base(name)] = [m for m in c.options["model"] if m != "default"]
        files = self._files()
        prompt = (
            "You are the lead of Claudex, a coding team of Claude Code and OpenAI Codex. You plan; workers do.\n"
            "Split the user's message into the FEWEST sequential steps (often one) and give each step to the best "
            "agent, model and reasoning level. Only one agent works at a time. Split only when parts clearly "
            "suit different agents (e.g. back end to Codex, then the page to Claude). Order steps so each builds "
            "on finished work (data, then logic, then screens). When parts connect, write the exact contract "
            "(endpoint, fields, function names) into BOTH steps' instructions. Each worker tests its own work and "
            "the other agent reviews it, so don't add separate testing or review steps.\n"
            "List in big_decisions ONLY choices that are hard to undo: database schema, a new dependency, "
            "sign-in/permissions, money or VAT logic, a contract between parts, deleting data, architecture. "
            "Usually there are none. Set browser=true only when the step must use the user's signed-in Chrome.\n"
            "You may read project files (read-only) to understand the codebase before planning.\n\n"
            f"USER MESSAGE:\n{text}\n\n"
            f"PROJECT FILES ({len(files)}):\n" + "\n".join(files[:300]) + "\n\n"
            f"{self._memory_block()}\n\n"
            f"ROUTING RULES (prefer these; explain any deviation in 'why'):\n{json.dumps(rules['rule'], indent=1)}\n"
            f"DEFAULT when no rule clearly fits: {json.dumps(rules['defaults'])}\n"
            f"MODELS EACH AGENT OFFERS NOW: {json.dumps(models)}\n"
            f"Only {rules['browser']['signed_in']} can use the user's signed-in browser.\n"
            "Write each step's instructions so the worker can act without seeing this message.\n")
        if critique:
            prompt += ("\nA SECOND OPINION on your earlier plan raised these concerns. Revise the plan where they "
                       "are right, and answer EVERY concern in concern_responses (accepted true/false and why):\n"
                       f"{json.dumps(critique, indent=1)}\n")
        else:
            prompt += "\nLeave concern_responses empty.\n"
        agent, model, effort = self._lead(rules)
        timeout = rules.get("limits", {}).get("plan_timeout_min", 10) * 60
        return await headless.ask(agent, self.cwd, prompt, PLAN_SCHEMA, model, effort, timeout)

    def _single(self, text, agent, rules, why):
        d = rules["agent_defaults"][agent]
        # Sent straight to Claude (@claude, "Claude only", a Claude skill): it gets the browser, as Claude
        # always has, since nobody planned whether it needs it.
        browser = agent in rules["browser"]["signed_in"]
        return {"steps": [{"title": "Your request", "agent": agent, "model": d["model"], "effort": d["effort"],
                           "browser": browser, "why": why, "instructions": text}],
                "big_decisions": [], "concern_responses": []}

    async def second_opinion(self, text, plan, rules):
        lead_agent = self._lead(rules)[0]
        who = other(lead_agent)
        r = rules["reviewers"][who]
        prompt = (
            "You are giving a second opinion on a plan another AI lead wrote, before any work starts. You may "
            "read the project files (read-only). Judge ONLY the big decisions: are they right for this codebase? "
            "Agree unless there is a concrete problem; don't nitpick wording or step order.\n\n"
            f"USER MESSAGE:\n{text}\n\nPLAN:\n{json.dumps(plan, indent=1)}\n\n{self._memory_block()}\n")
        timeout = rules.get("limits", {}).get("check_timeout_min", 15) * 60
        return who, r, await headless.ask(who, self.cwd, prompt, OPINION_SCHEMA, r["model"], r["effort"], timeout)

    # ---------- one message ----------
    async def turn(self, text):
        self.cancelled, self.ok = False, True
        self._task = asyncio.current_task()
        rules = load_rules(self.rules_path)
        if not self.dry_run and self.reviews_on and not gitsnap.is_repo(self.cwd):
            self.ok = False
            await self.ui.say("This folder isn't a git project, so Claudex can't see what the workers change and "
                              "can't check their work. Run `git init` here first, or turn checks off "
                              "(`--checks off`, or Checks: No checks in Agents of Empires).\n")
            return "end_turn"
        self._prepare_memory()
        work_lock = None if self.dry_run else Lock(self.root / "work.lock")
        try:
            if work_lock:
                await work_lock.acquire(lambda: self.ui.say(
                    "_Another Claudex job is working in this project; waiting for it to finish…_\n\n"))
            self._job = {"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "request": text, "rows": []}
            stop = await self._turn(text, rules)
            self._write_journal("done" if self.ok and stop == "end_turn" else "stopped")
            self._act(None, "done" if self.ok and stop == "end_turn" else "stopped")
            return stop
        except asyncio.CancelledError:
            self._clear_our_cancel()
            self.ok, self.cancelled = False, True
            self._write_journal("stopped")
            self._act(None, "stopped")
            await self.ui.say("\n\n_Stopped._\n")
            return "cancelled"
        except Exception:
            self.ok = False
            self._write_journal("failed")
            self._act(None, "stopped")
            raise
        finally:
            self._job = None
            self._clear_our_cancel()
            self._task = None
            if work_lock:
                work_lock.release()

    async def _turn(self, text, rules):
        forced = self.route if self.route != "auto" else None
        for tag in ("claude", "codex"):
            if text.lstrip().lower().startswith("@" + tag):
                forced, text = tag, text.lstrip()[len(tag) + 1:].lstrip()
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self._append(self.notes, f"\n# {stamp} · Request\n{text}\n\n")

        if forced:
            plan = self._single(text, forced, rules, f"you chose {forced.title()} only")
        else:
            agent, model, effort = self._lead(rules)
            self._act("lead", "thinking")
            await self.ui.say(f"_{fun.pick(fun.PLANNING)}… (lead: {agent.title()} {model}, {effort})_\n\n")
            try:
                plan = await self.plan(text, rules)
            except Exception as e:
                d = rules["defaults"]
                await self.ui.say(f"_(The lead couldn't plan this: {e}. Using the default worker.)_\n\n")
                plan = self._single(text, d["agent"], rules, "default (the lead failed)")
                plan["steps"][0].update(model=d["model"], effort=d["effort"])
        if self.cancelled:
            return "cancelled"

        if plan.get("big_decisions") and self.second_on and not forced:
            plan = await self._check_decisions(text, plan, rules)
            if plan is None:
                self.ok = False
                return "end_turn"
            if self.cancelled:
                return "cancelled"

        steps = plan["steps"]
        for s in steps:  # a step needing the signed-in browser must go to an agent that has it
            if s["browser"] and s["agent"] not in rules["browser"]["signed_in"]:
                s["agent"] = rules["browser"]["signed_in"][0]
                s["model"] = rules["agent_defaults"][s["agent"]]["model"]
                s["why"] += " (moved: needs the signed-in browser)"
            if self.effort != "auto":
                s["effort"] = self.effort
        entries = [{"content": f"{s['title']} · {s['agent'].title()} {s['model']} {s['effort']}"
                               + (" · browser" if s["browser"] else ""),
                    "priority": "medium", "status": "pending"} for s in steps]
        await self.ui.plan(entries)
        if self.dry_run:
            for i, s in enumerate(steps, 1):
                await self.ui.say(f"**{i}. {s['title']}**: {s['agent'].title()} {s['model']} {s['effort']}"
                                  f"{' · browser' if s['browser'] else ''}  \n_{s['why']}_\n\n")
            await self.ui.say("_(Dry run: nothing was changed.)_\n")
            return "end_turn"

        turn_start = gitsnap.snapshot(self.project_root)
        if turn_start is None and self.reviews_on:
            self.ok = False
            await self.ui.say("**Stopped before any work:** couldn't take a snapshot of the project files (git failed).\n")
            return "end_turn"
        changed_any = False
        for i, s in enumerate(steps, 1):
            if self.cancelled:
                return "cancelled"
            entries[i - 1]["status"] = "in_progress"
            await self.ui.plan(entries)
            try:
                try:
                    changed = await self._run_step(i, len(steps), s, text, rules)
                except gitsnap.SnapshotError as e:
                    raise StepFailed(f"couldn't compare the files before and after ({e}).")
                changed_any = changed_any or changed
            except StepFailed as e:
                self.ok = False
                entries[i - 1]["status"] = "completed"
                await self.ui.plan(entries)
                await self.ui.say(f"\n\n**Stopped at step {i}:** {e}\n\n")
                self._append(self.notes, f"## Stopped at step {i}\n{e}\n\n")
                return "cancelled" if self.cancelled else "end_turn"
            entries[i - 1]["status"] = "completed"
            await self.ui.plan(entries)

        if changed_any:
            turn_end = gitsnap.snapshot(self.project_root)
            if turn_end is None:
                self.ok = False
                await self.ui.say("**Couldn't take the final snapshot (git failed), so the final check and project "
                                  "map were skipped.**\n")
                return "end_turn"
            if len(steps) > 1 and self.reviews_on and rules.get("final_check", {}).get("enabled", True):
                await self._final_check(text, steps, turn_start, turn_end, rules)
            if rules.get("memory", {}).get("update_map", True):
                await self._update_map(text, turn_start, turn_end, rules)
        return "end_turn"

    async def _check_decisions(self, text, plan, rules):
        decisions = plan["big_decisions"]
        await self.ui.say("**Big decisions in this plan:**\n" +
                          "".join(f"- {d['decision']} ({d['why']})\n" for d in decisions) + "\n")
        self._act(other(self._lead(rules)[0]), "checking", "lead")
        try:
            who, r, op = await self.second_opinion(text, plan, rules)
        except Exception as e:
            self._log(kind="second_opinion", verdict="unavailable", error=str(e)[:300])
            if self._carry_on(rules):
                await self.ui.say(f"_(Second opinion unavailable: {e}. Carrying on, as the rules allow.)_\n\n")
                return plan
            await self.ui.say(f"**Stopped before any work:** the second opinion couldn't run ({e}). "
                              "Run `claudex doctor`, or use `--no-second-opinion`.\n")
            return None
        await self.ui.say(f"_Second opinion from {who.title()} {r['model']} ({r['effort']}): "
                          + (f"{fun.pick(fun.AGREE)}_\n\n" if op["verdict"] == "agree" else "some concerns._\n"))
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        record = f"\n## {stamp} · {text[:120]}\n" + "".join(
            f"- **Decision:** {d['decision']}. Why: {d['why']}. Alternatives: {d['alternatives']}\n" for d in decisions)
        record += f"- **Second opinion ({who} {r['model']} {r['effort']}):** {op['verdict']}\n"
        if op["verdict"] == "concerns" and op["concerns"]:
            await self.ui.say("".join(f"- {c}\n" for c in op["concerns"]) + "\n_Re-planning with these in mind…_\n\n")
            record += "".join(f"  - Concern: {c}\n" for c in op["concerns"])
            try:
                revised = await self.plan(text, rules, critique={"concerns": op["concerns"],
                                                                 "suggestion": op["suggestion"],
                                                                 "earlier_plan": plan})
                plan = revised
                for cr in plan.get("concern_responses", []):
                    verdict = "accepted" if cr["accepted"] else "kept the original"
                    record += f"  - Lead {verdict}: {cr['concern']}. {cr['why']}\n"
                    await self.ui.say(f"- Lead {verdict}: {cr['concern']}. _{cr['why']}_\n")
                await self.ui.say("\n")
            except Exception as e:
                self._append(self.decisions, record + f"  - Re-plan failed: {e}\n")
                await self.ui.say(f"**Stopped before any work:** the lead couldn't re-plan after the second "
                                  f"opinion's concerns ({e}).\n")
                return None
        self._append(self.decisions, record)
        self._log(kind="second_opinion", reviewer=who, verdict=op["verdict"], concerns=op.get("concerns", []))
        return plan

    @staticmethod
    def _carry_on(rules):
        return rules.get("checks", {}).get("if_unavailable", "stop") == "continue"

    def _brief(self, i, n, s, text):
        return (
            f"You are one worker in Claudex, a team where Claude Code and Codex take turns on a job.\n"
            f"Step {i} of {n}: {s['title']}\n\n{s['instructions']}\n\n"
            f"The user's original message:\n{text}\n\n{self._memory_block()}\n\n"
            "Do only this step. Check your own work where you can (run the tests). Do not commit unless asked. "
            "The other agent will review your changes afterwards.\n"
            f"If you hit a hard-to-undo decision the plan didn't settle (database schema, new dependency, "
            f"sign-in, money logic, deleting data), stop and write a line starting '{DECISION_TAG}' explaining "
            "the choice and options, instead of guessing.\n"
            "If you changed files, end with a short handoff: what you changed (files), what you checked and the "
            "result, and anything the next agent must know. If you only answered a question, just answer it.")

    async def _configure(self, c, s, rules, model, effort):
        agent = base(c.name)
        if not await c.set("model", model):
            fallback = rules["agent_defaults"][agent]["model"]
            if not await c.set("model", fallback):
                if c.options.get("model"):
                    raise StepFailed(f"{agent.title()} won't accept model {model} or {fallback}. "
                                     "Run `claudex doctor`.")
            else:
                await self.ui.say(f"_({model} isn't offered right now; using {fallback}.)_\n\n")
                model = fallback
        if not await c.set(rules["effort_option"][agent], effort) and c.options.get(rules["effort_option"][agent]):
            await self.ui.say(f"_({agent.title()} doesn't offer {effort} reasoning on {model}; using its default.)_\n\n")
        wanted = MODES[self.mode][1][agent]
        if c.options.get("mode") and not await c.set("mode", wanted):
            raise StepFailed(f"couldn't set {agent.title()}'s permission mode to {wanted}, so it wasn't started.")
        return model

    async def _run_step(self, i, n, s, text, rules):
        """Run one step with review and escalation. Returns True if files changed. Raises StepFailed."""
        name = "claude_browser" if s["browser"] else s["agent"]
        agent = s["agent"]
        chrome = Lock(CHROME_LOCK) if s["browser"] else None
        if chrome:
            await chrome.acquire(lambda: self.ui.say("_Waiting for Chrome: another agent is using it…_\n\n"))
        try:
            try:
                c = await self.child(name, rules)
            except Exception as e:
                raise StepFailed(f"couldn't start {agent.title()}: {e}")
            model, effort = s["model"], s["effort"]
            review = rules.get("review", {})
            max_attempts = int(review.get("max_attempts", 3))
            step_timeout = rules.get("limits", {}).get("step_timeout_min", 60) * 60
            before = gitsnap.snapshot(self.project_root)
            if before is None and self.reviews_on:
                raise StepFailed("couldn't take a snapshot of the project files (git failed), so the work "
                                 "couldn't be checked.")
            message = self._brief(i, n, s, text)
            for attempt in range(1, max_attempts + 1):
                if self.cancelled:
                    raise StepFailed("cancelled.")
                try:
                    model = await asyncio.wait_for(self._configure(c, s, rules, model, effort), START_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._stop_child(name)
                    raise StepFailed(f"{agent.title()} didn't accept its settings in time.")
                head = f"**Step {i}/{n} · {agent.title()} · {model} · {effort}**"
                if attempt == 1:
                    await self.ui.say(f"{head}  \n_{s['title']}: {s['why']}_\n\n")
                else:
                    await self.ui.say(f"{head} · attempt {attempt}\n\n")
                started = asyncio.get_running_loop().time()
                self._act(agent, "working")
                self.active = c
                try:
                    stop, out = await asyncio.wait_for(c.prompt(message), step_timeout)
                except asyncio.TimeoutError:
                    c.cancel()
                    await self._stop_child(name)  # make sure it has really stopped before moving on
                    raise StepFailed(f"{agent.title()} ran past the {step_timeout // 60}-minute limit.")
                except asyncio.CancelledError:
                    await self._stop_child(name)
                    raise
                except Exception as e:
                    await self._stop_child(name)
                    raise StepFailed(f"{agent.title()} stopped with an error: {e}")
                finally:
                    self.active = None
                secs = round(asyncio.get_running_loop().time() - started, 1)
                await self.ui.say("\n\n")
                self._append(self.notes, f"## Step {i}: {s['title']} ({agent} {model} {effort}, attempt {attempt})"
                                         f"\n{out}\n\n")
                row = dict(kind="step", step=i, title=s["title"], agent=agent, model=model, effort=effort,
                           attempt=attempt, secs=secs, stop=stop, browser=s["browser"],
                           handoff=tail(out.strip(), HANDOFF_LIMIT))
                if stop == "cancelled" or self.cancelled:
                    self._log(**row)
                    self.cancelled = True
                    raise StepFailed("cancelled.")
                if stop == "refusal":
                    self._log(**row, verdict="refused")
                    raise StepFailed(f"{agent.title()} refused to do this step.")
                if stop in ("max_tokens", "max_turn_requests"):
                    self._log(**row, verdict=stop)
                    raise StepFailed(f"{agent.title()} ran out of room before finishing ({stop}).")
                if DECISION_TAG in out:
                    self._log(**row, verdict="decision_needed")
                    reason = out[out.index(DECISION_TAG):].splitlines()[0]
                    raise StepFailed(f"the worker needs a decision from you. {reason}")
                after = gitsnap.snapshot(self.project_root)
                if after is None and self.reviews_on:
                    raise StepFailed("couldn't take a snapshot of the project files after the step (git failed).")
                diff, files, truncated = gitsnap.diff(self.project_root, before, after,
                                                      int(review.get("diff_limit", 60000)))
                if not files:
                    self._log(**row, verdict="no_changes")
                    return False
                if (not self.reviews_on or not review.get("enabled", True)
                        or (review.get("skip_low_effort", False) and effort == "low")):
                    self._log(**row, verdict="not_reviewed", files=files)
                    return True
                verdict = await self._review(s, text, out, diff, files, truncated, rules)
                self._log(**row, verdict=verdict["verdict"], issues=verdict["issues"], files=files)
                if self.cancelled:
                    raise StepFailed("cancelled.")
                if verdict["verdict"] in ("pass", "unreviewed"):
                    return True
                if attempt == max_attempts:
                    issues = "".join(f"\n- {x}" for x in verdict["issues"])
                    raise StepFailed(f"the review still found problems after {attempt} attempts:{issues}")
                if attempt >= 2:
                    up = next_rung(rules.get("ladder", {}), agent, model, effort)
                    if up:
                        model, effort = up
                        await self.ui.say(f"_Stepping up to {model} {effort}._\n\n")
                message = ("The reviewer (the other agent) checked your changes and asks for these fixes:\n"
                           + "".join(f"- {x}\n" for x in verdict["issues"])
                           + "\nFix them, re-check your work, and end with the same short handoff.")
        finally:
            if chrome:
                chrome.release()

    async def _review(self, s, text, out, diff, files, truncated, rules):
        who = other(s["agent"])
        r = rules["reviewers"][who]
        self._act(who, "reviewing", s["agent"])
        await self.ui.say(f"_{fun.pick(fun.REVIEWING)}: {who.title()} {r['model']} ({r['effort']})…_ ")
        prompt = (
            "You are reviewing changes another AI agent just made, as a strict senior engineer. You may read "
            "project files (read-only). Say 'fix' only for real problems: bugs, missed requirements, broken "
            "contracts with other parts, security issues, or untested risky logic. Don't ask for style changes. "
            "Each issue must be concrete and actionable.\n\n"
            f"USER MESSAGE:\n{text}\n\nTHE STEP IT WAS GIVEN:\n{s['title']}\n{s['instructions']}\n\n"
            f"SHARED PROJECT MEMORY (judge against these rules and decisions too):\n{self._memory_block()}\n\n"
            f"WORKER'S HANDOFF:\n{tail(out, 4000)}\n\n{SNAPSHOT_NOTE}\nFILES CHANGED: {', '.join(files)}\n\n"
            + ("NOTE: the diff below is cut short; read the remaining changed files directly before judging.\n"
               if truncated else "") + f"DIFF:\n{diff}\n")
        timeout = rules.get("limits", {}).get("check_timeout_min", 15) * 60
        try:
            v = await headless.ask(who, self.cwd, prompt, REVIEW_SCHEMA, r["model"], r["effort"], timeout)
        except Exception as e:
            if self._carry_on(rules):
                await self.ui.say(f"\n_(Review unavailable: {e}. Carrying on unreviewed, as the rules allow.)_\n\n")
                return {"verdict": "unreviewed", "issues": [], "summary": f"review unavailable: {e}"}
            raise StepFailed(f"the review couldn't run ({e}). The changes are in place but unchecked. "
                             "Run `claudex doctor`.")
        self._act(who, "pass" if v["verdict"] == "pass" else "fix", s["agent"])
        if v["verdict"] == "pass":
            await self.ui.say(f"passed. {v['summary']}\n\n")
        else:
            await self.ui.say("found problems:\n" + "".join(f"- {x}\n" for x in v["issues"]) + "\n")
        self._append(self.notes, f"### Review ({who} {r['model']} {r['effort']}): {v['verdict']}\n"
                                 + "".join(f"- {x}\n" for x in v["issues"]) + f"{v['summary']}\n\n")
        return v

    async def _final_check(self, text, steps, start, end, rules):
        agent, model, effort = self._lead(rules)
        try:
            diff, files, truncated = gitsnap.diff(self.project_root, start, end, int(rules.get("review", {}).get("diff_limit", 60000)))
        except gitsnap.SnapshotError as e:
            self.ok = False
            await self.ui.say(f"**Final check couldn't run:** {e}\n")
            return
        self._act("lead", "thinking")
        await self.ui.say(f"_Final check by the lead ({agent.title()} {model})…_ ")
        prompt = (
            "You led this job. All steps are done and each was reviewed. Check that the parts fit together and "
            "the user's request is fully met. You may read project files (read-only).\n\n"
            f"USER MESSAGE:\n{text}\n\nPLAN:\n{json.dumps(steps, indent=1)}\n\n"
            f"SHARED PROJECT MEMORY:\n{self._memory_block()}\n\n"
            f"{SNAPSHOT_NOTE}\nFILES CHANGED: {', '.join(files)}\n"
            + ("NOTE: the diff is cut short; read the remaining files directly.\n" if truncated else "")
            + f"DIFF:\n{diff}\n")
        timeout = rules.get("limits", {}).get("check_timeout_min", 15) * 60
        try:
            v = await headless.ask(agent, self.cwd, prompt, FINAL_SCHEMA, model, effort, timeout)
        except Exception as e:
            self.ok = self.ok and self._carry_on(rules)
            await self.ui.say(f"\n**Final check couldn't run:** {e}\n\n")
            self._log(kind="final_check", verdict="unavailable", error=str(e)[:300])
            return
        if v["verdict"] == "pass":
            await self.ui.say(f"all good. {v['summary']}\n\n")
        else:
            self.ok = False
            await self.ui.say("**problems left:**\n" + "".join(f"- {x}\n" for x in v["problems"]) + "\n")
        self._append(self.notes, f"### Final check: {v['verdict']}\n" + "".join(f"- {x}\n" for x in v["problems"])
                                 + f"{v['summary']}\n\n")
        self._log(kind="final_check", verdict=v["verdict"], problems=v["problems"])

    async def _update_map(self, text, start, end, rules):
        m = rules.get("memory", {}).get("map_model", {"agent": "codex", "model": "gpt-6.1-sol", "effort": "low"})
        try:
            files = gitsnap.changed_files(self.project_root, start, end)
        except gitsnap.SnapshotError:
            files = []
        prompt = (
            "Update the project map: a short guide (under 150 lines, markdown) for AI agents to this codebase: "
            "layout, key files and what they do, data/database, how it runs, tests and deploys. Keep what is "
            "still true, add what this job changed, remove what is no longer true. You may read files.\n\n"
            f"CURRENT MAP:\n{self._read(self.map, 20000)}\n\nTHIS JOB: {text[:2000]}\n"
            f"FILES CHANGED: {', '.join(files)}\n\n"
            f"SHARED PROJECT MEMORY (keep anything here that future agents need in the map):\n{self._memory_block()}\n")
        try:
            v = await headless.ask(m["agent"], self.cwd, prompt, MAP_SCHEMA, m["model"], m["effort"], 600)
        except Exception as e:
            await self.ui.say(f"_(Project map not updated: {e}.)_\n")
            return
        tmp = self.map.with_suffix(".tmp")
        tmp.write_text(v["map"].rstrip() + "\n")
        os.replace(tmp, self.map)
