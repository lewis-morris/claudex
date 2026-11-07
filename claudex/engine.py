"""Claudex: one lead plans each job, big decisions get a second opinion, one worker at a time does each
part, the opposite side reviews every change, and everything lands in shared project memory."""
import asyncio
import datetime
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import subprocess
import time
import tomllib
import uuid

from . import fun, gitsnap, headless, permissions, usage, project_settings
from .child import Child
from .ladder import next_rung
from .locks import CHROME_LOCK, Lock

HOME = pathlib.Path(__file__).resolve().parent.parent
from .paths import rules_file
CATALOG = pathlib.Path.home() / ".config/agent-of-empires/acp_option_catalog.json"
NOTES_LIMIT = 8000
MEMORY_LIMIT = 6000
JOURNAL_LIMIT = 12000
INSTRUCTIONS_LIMIT = 40000   # per file; house rules are never cut silently
HANDOFF_LIMIT = 1500         # each worker's handover kept for the rest of the job and in the journal
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")  # Claude reads one, Codex the other; Claudex shows both to everyone
# How much of its own file each agent reads by itself (checked with a canary word at the end of a long file):
# Codex stops at about 32 KiB of AGENTS.md; Claude Code reads a 50 KB CLAUDE.md whole. Over the cap, it's pasted.
NATIVE_LIMIT = {"codex": 32000, "claude": None}
DECISION_TAG = "DECISION NEEDED:"
SNAP_HELP = ("Claudex needs git installed to compare files (`git --version`). Run `claudex doctor` to check, "
             "or use `--checks off` to work without checks.")
START_TIMEOUT = 120  # seconds for a worker to start, or to accept its settings
SNAPSHOT_NOTE = ("(File lists and diffs come from Claudex's own snapshots of the working folder. Nothing has "
                 "been staged or committed in the user's git.)")

ROUTES = {"auto": "Auto (Claudex decides)", "claude": "Claude only", "codex": "Codex only"}
EFFORTS = ["auto", "low", "medium", "high", "xhigh"]
CHECKS = {"full": "Second opinions + reviews", "reviews": "Reviews only", "off": "No checks"}
# Claudex mode -> each worker's own permission mode.
MODES = {
    # Claudex mode -> (label, each worker's own mode). In "ask" and "auto" the workers ask before acting and
    # Claudex answers (you, or its rules + gatekeeper). In "unrestricted" they don't ask at all.
    "ask": ("Ask me", {"claude": "default", "codex": "read-only"}),
    "auto": ("Auto (Claudex rules + gatekeeper)", {"claude": "default", "codex": "read-only"}),
    "unrestricted": ("Unrestricted", {"claude": "bypassPermissions", "codex": "agent-full-access"}),
}
LEGACY_MODES = {"default": "ask", "acceptEdits": "auto", "bypassPermissions": "unrestricted"}


# Files whose change can alter how the project is laid out, built, run or tested: worth a map refresh.
MAP_FILES = {"package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "cargo.toml", "go.mod",
             "gemfile", "composer.json", "makefile", "dockerfile", "procfile", "tsconfig.json", "manage.py",
             "claude.md", "agents.md", "readme.md"}
MAP_PATTERNS = ("docker-compose*", "*.config.js", "*.config.ts", "*.config.mjs", "requirements*.txt",
                ".github/workflows/*", "deploy*", "*.sql", "*migrations/*", "*.toml")


def map_worthy(changes):
    """True when a job added, deleted or renamed files, or touched how the project is built, run or deployed."""
    for status, f in changes:
        if status[:1] in ("A", "D", "R", "C"):
            return True
        path = pathlib.PurePosixPath(f)
        if path.name.lower() in MAP_FILES or any(path.match(pat) or fnmatch.fnmatch(f, pat) for pat in MAP_PATTERNS):
            return True
    return False


def human_actions(text):
    """Things the user must answer or do. Reads the structured 'Questions for you' section (one item per
    'Q<n>. Title' plus its question line, and each numbered line under 'Things only you can do'), and still
    accepts the older plain 'NEEDS YOU:' marker and Markdown 'Needs you:' headings."""
    items, current, fenced, section, question = [], [], False, None, None

    def flush_question():
        nonlocal question
        if question:
            items.append(" ".join(question).strip())
            question = None

    for line in text.splitlines() + [""]:
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced:
            continue
        bare = re.sub(r"[#*_`]", "", line).strip().lower().rstrip(":")
        if bare in ("questions for you", "things only you can do"):
            flush_question()
            section = bare
            continue
        if section and re.match(r"^\s*#{1,6}\s", line) and "question" not in bare and "only you" not in bare \
                and not re.match(r"^[a-z &/'-]{1,60}$", bare):
            flush_question()
            section = None
        if section == "questions for you":
            q = re.match(r"^\s*(?:[-*]\s+|#{1,6}\s+)?\*{0,2}Q(\d+)\.\s*(.+?)\*{0,2}\s*$", line)
            if q:
                flush_question()
                question = [f"Q{q.group(1)}. {q.group(2).strip('* ')}:"]
            elif question and len(question) == 1 and line.strip() and not re.match(
                    r"^\s*(?:[-*]\s+)?(?:\*\*)?(?:[A-F][).:]|why|if you|options|reply like)", line, re.I):
                question.append(re.sub(r"^\s*[-*]\s+", "", line).strip())
            continue
        if section == "things only you can do":
            t = re.match(r"^\s*(?:\d+[.)]|[-*])\s+(.+)", line)
            if t:
                items.append(t.group(1).strip())
            continue
        match = re.match(r"^\s*(?:[-*]\s+|#{1,6}\s+)?(?:\*\*)?needs you(?:\*\*)?\s*:(?:\*\*)?\s*(.*)",
                         line, re.I)
        if match or not line.strip():
            if current:
                item = " ".join(current).strip()
                if item.lower().rstrip(".") not in ("none", "nothing", "not needed"):
                    items.append(item)
                current = []
            if match:
                current = [match.group(1)]
        elif current:
            current.append(line.strip())
    flush_question()
    return items


def norm_mode(mode):
    mode = LEGACY_MODES.get(mode, mode)
    return mode if mode in MODES else "auto"

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
# One shape for every worker's reply, whichever agent wrote it: easier to read, and shorter for the checker.
QUESTIONS_SHAPE = (
    "If the user must answer something or do a manual task, end your reply with a section headed "
    "'Questions for you' (only work the human must do, never your own tasks; carry on with independent work). "
    "Start it with one line: 'Reply like: 1A, 2B, 3 skip, 5: <your words>'. Then number the questions, grouped "
    "under short topic headings, with anything that blocks work or carries a security risk first. Each "
    "question is: a bold line 'Q<n>. Short title'; the question in one plain sentence; 'Why: ' what it matters "
    "for or blocks, in one line; lettered options 'A) ', 'B) ', 'C) ', each short, with one marked "
    "'(recommended)' and a few words of reason (free text only where options make no sense, then list the "
    "likely candidates); and 'If you don't answer: ' the safe default or 'waits'. Put several 'which customer "
    "or owner is this?' items in one table (Item | Best guess | Confirm Y/N or name) under one question. Put "
    "tasks only the human can do under a heading 'Things only you can do', one numbered line each, saying "
    "where. Drop questions that are already answered; mark repeats '(still open)'.")
REPLY_SHAPE = (
    "Your reply is shown to the user as it is, and the other agent checks it before they act on it. If you "
    "changed files, end with this, in plain British English, leaving out any line that would be empty:\n"
    "**Done:** one or two sentences on the outcome.\n"
    "**Changed:** a short bullet per file: what changed (a table only if before/after needs comparing).\n"
    "**Checked:** what you ran or read to confirm it, and the result.\n"
    "**Not checked:** anything you couldn't confirm, or assumed.\n"
    "Put anything for the user in the 'Questions for you' section below, not in these lines.\n"
    "No preamble, no restating the request, no closing offers; keep it under about 250 words unless a table "
    "is needed. If you only answered a question, just answer it.")
REFUSAL_RETRIES = 3       # how many times a worker whose turn ended on a 'no' is told why and carries on
DOC_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc"}
REVIEW_SCHEMA = {"type": "object", "properties": {
    "verdict": {"enum": ["pass", "fix"]}, "issues": {"type": "array", "items": {"type": "string"}},
    "summary": {"type": "string"}}}
FINAL_SCHEMA = {"type": "object", "properties": {
    "verdict": {"enum": ["pass", "problems"]}, "problems": {"type": "array", "items": {"type": "string"}},
    "summary": {"type": "string"}}}
MAP_SCHEMA = {"type": "object", "properties": {"map": {"type": "string"}}}


def _merge(base_, over):
    out = dict(base_)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_rules(path=None, project_root=None, models_file=None, model_preferences=True):
    """Your rules (or the defaults), with the project's own .claudex/rules.toml layered on top if it has one."""
    rules = tomllib.loads(pathlib.Path(path or rules_file()).read_text())
    if project_root:
        own = pathlib.Path(project_root) / ".claudex" / "rules.toml"
        if own.is_file():
            extra = tomllib.loads(own.read_text())
            perms = extra.pop("permissions", {}) or {}
            rules = _merge(rules, extra)
            # A project can only ADD permission rules, never remove yours: deny/ask/allow lists are joined, and
            # because deny beats ask beats allow, a project's "allow" can't undo your "deny" or "ask".
            base_perms = dict(rules.get("permissions", {}))
            for k, v in perms.items():
                if k in ("deny", "ask", "allow") and isinstance(v, list):
                    base_perms[k] = list(base_perms.get(k, [])) + [x for x in v if x not in base_perms.get(k, [])]
                elif k not in ("deny", "ask", "allow"):
                    base_perms[k] = v
            rules["permissions"] = base_perms
    from . import models
    rules = models.apply(rules, models.find(project_root, models_file))
    return project_settings.apply_models(rules, project_settings.load(project_root)) if project_root and model_preferences else rules


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
    """The last `limit` characters, starting at a whole entry (a '#' heading) or at least a whole line,
    so what's kept never opens mid-sentence."""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    m = re.search(r"(?m)^#{1,3} ", cut)
    if m and m.start() <= len(cut) // 2:
        cut = cut[m.start():]
    elif "\n" in cut[:len(cut) // 2]:
        cut = cut[cut.index("\n") + 1:]
    return "…(earlier text trimmed)…\n" + cut


def repo_root(cwd):
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True,
                             timeout=10)
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


class StepFailed(Exception):
    pass


class WorkerLimited(Exception):
    """A worker hit its usage limit partway through a step; the step can go to the other agent."""

    def __init__(self, agent, said, before=None):
        super().__init__(f"{agent} is at its usage limit")
        self.agent, self.said, self.before = agent, said, before   # before: the step's starting snapshot


from .limits import error_is_limit, reply_is_limit, reset_hint  # noqa: E402


class Engine:
    def __init__(self, cwd, ui, mcp_servers=None, settings=None, session_key=None):
        self.refusals = []          # actions Claudex said no to (so a worker can be told why)
        self.cwd = str(cwd)
        self.project_root = repo_root(self.cwd) or self.cwd
        saved = project_settings.load(self.project_root)
        s = {**saved, **{k: v for k, v in (settings or {}).items() if v is not None}}
        self.cwd = str(cwd)
        self.ui = ui
        self.mcp_servers = mcp_servers or []
        self.rules_path = s.get("rules")
        self.resume_request = s.get("resume")       # "pick", a number, or None (the screen acts on it)
        self.models_file = s.get("models")
        self.route = s.get("route", "auto")
        self.effort = s.get("effort", "auto")
        self.mode = norm_mode(s.get("mode", "unrestricted"))
        self.unattended = bool(s.get("unattended"))   # nobody to ask: "ask the human" becomes "no"
        self._human_waits = 0
        self._human_since = None
        self._human_elapsed = 0.0
        self._rules = None
        self.checks = s.get("checks", "full")
        self.dry_run = s.get("dry_run", False)
        self.lead_model, self.lead_effort = s.get("lead_model"), s.get("lead_effort")
        self.children = {}
        self.active = None
        self.cancelled = False
        self.ok = True
        self.limited = {}           # agent -> (when we noticed, what it said about resetting)
        self._planned_by = None     # who actually wrote the plan (the lead, or the other side if it was limited)
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

    async def switch_session(self, key):
        """Carry on an earlier conversation: stop this one's workers, then use that one's notes and workers."""
        await self.close()
        self.children = {}
        self.use_session(key)

    def use_session(self, key):
        """Carry on an earlier conversation: its notes, log, handovers and workers become this session's."""
        self.session_key = key
        self.notes_dir = self.root / "sessions" / key
        self.notes = self.notes_dir / "notes.md"
        self.log = self.notes_dir / "steps.jsonl"

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
        return tail(path.read_text(errors="replace"), limit) if path.exists() else "(none yet)"

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

    def _act(self, who, state, target=None, model=None, effort=None):
        """Tell a screen that wants to know (the full-screen UI) who is doing what right now, on which model."""
        f = getattr(self.ui, "activity", None)
        if f:
            try:
                f(who, state, target, f"{model} · {effort}" if model else None)
            except TypeError:
                f(who, state, target)
            except Exception:
                pass

    def _native(self, agent):
        """The instruction file this agent already loads by itself (Claude Code reads CLAUDE.md, Codex reads
        AGENTS.md), so it needn't be pasted in again. None when unknown, or when switched off in the rules."""
        if not agent or not (self._rules or {}).get("memory", {}).get("native_instructions", True):
            return None
        return {"claude": "CLAUDE.md", "codex": "AGENTS.md"}.get(base(agent))

    def _instructions(self, agent=None):
        """The project's own agent instructions. Claude only reads CLAUDE.md and Codex only AGENTS.md, so both
        are shown to everyone: one set of house rules for the whole team. The file an agent loads by itself
        is named, not repeated."""
        parts, seen, native = [], set(), self._native(agent)
        for folder in dict.fromkeys([self.project_root, self.cwd]):
            for name in INSTRUCTION_FILES:
                f = pathlib.Path(folder) / name
                if not f.is_file() or f.resolve() in seen:
                    continue
                seen.add(f.resolve())
                cap = NATIVE_LIMIT.get(base(agent)) if agent else None
                if name == native and (cap is None or f.stat().st_size <= cap):
                    parts.append(f"--- {f} ---\n(You already have this file: {base(agent).title()} loads it "
                                 "itself. Follow it.)")
                    continue
                text = f.read_text(errors="replace")
                if len(text) > INSTRUCTIONS_LIMIT:   # never drop rules silently: say so, and where the rest is
                    text = (text[:INSTRUCTIONS_LIMIT] + f"\n[… {f} continues. Read the whole file before you start; "
                            "its rules all apply.]")
                parts.append(f"--- {f} ---\n{text}")
        saved = project_settings.load(self.project_root)
        for key, title in (("description", "PROJECT BRIEF"), ("base_prompt", "PROJECT BASE PROMPT")):
            if saved.get(key):
                parts.append(f"--- {title} ---\n{saved[key]}")
        return "\n".join(parts) or "(none)"

    def _notes_before_job(self):
        """This session's notes from before the current job. The current job's replies are already in
        THIS JOB SO FAR, so they aren't sent twice."""
        if not self.notes.exists():
            return "(none yet)"
        raw = self.notes.read_bytes()
        upto = (self._job or {}).get("notes_at")
        text = (raw if upto is None else raw[:upto]).decode("utf-8", "replace")
        return tail(text, NOTES_LIMIT) if text.strip() else "(none yet)"

    def _memory_parts(self, agent=None):
        """The shared memory as (key, title, text), steadiest first so repeated calls share a cached start."""
        return [
            ("instructions", "PROJECT INSTRUCTIONS (CLAUDE.md and AGENTS.md; follow both)", self._instructions(agent)),
            ("map", "PROJECT MAP", self._read(self.map, MEMORY_LIMIT)),
            ("decisions", "PAST DECISIONS", self._read(self.decisions, MEMORY_LIMIT)),
            ("journal", f"RECENT JOBS IN THIS PROJECT (all sessions; older ones are in {self.journal})",
             self._read(self.journal, JOURNAL_LIMIT)),
            ("notes", "THIS SESSION'S EARLIER NOTES", self._notes_before_job()),
            ("job", "THIS JOB SO FAR (every handover and check, never trimmed)", self._job_so_far()),
        ]

    # Who needs what. Old jobs and session chatter help the planner and the workers; a check of a diff
    # needs the rules, the decisions, the map and this job.
    ALL_MEMORY = ("instructions", "map", "decisions", "journal", "notes", "job")
    CHECK_MEMORY = ("instructions", "map", "decisions", "job")

    def _memory_block(self, parts=ALL_MEMORY, agent=None):
        """The one shared memory, or the slice of it a role needs."""
        return "\n\n".join(f"{title}:\n{text}" for key, title, text in self._memory_parts(agent) if key in parts)

    def _worker_memory(self, c):
        """The memory for a worker's next prompt. Workers keep one conversation across steps and jobs, so a
        part it has already been sent unchanged is named instead of pasted again. Everything is sent again
        when the conversation is new to this Claudex, or looks squashed (its context shrank)."""
        sent = getattr(c, "memory_sent", None)
        job = (self._job or {}).get("id")
        squashed = getattr(c, "squashed", False)   # the worker's context shrank at some point since the last brief
        blind = (getattr(c, "usage", None) or {}).get("used") is None   # it never says, so squashes can't be seen
        if sent is None or squashed or (blind and getattr(c, "memory_job", None) != job):
            sent = {}
        c.squashed, c.memory_job = False, job
        out, now = [], {k: v for k, v in sent.items() if k == "shapes"}   # the reply shape is tracked too
        for key, title, text in self._memory_parts(base(c.name)):
            digest = hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()
            now[key] = digest
            if key != "job" and sent.get(key) == digest:
                out.append(f"{title}: unchanged since you were last given it earlier in this conversation.")
            else:
                out.append(f"{title}:\n{text}")
        c.memory_sent = now
        return "\n\n".join(out)

    def _keep_handover(self, i, attempt, agent, out):
        """Save the whole handover to a file; memory gets its start and end (warnings are often up front)
        plus where to read the rest."""
        out = out.strip()
        if len(out) <= HANDOFF_LIMIT * 2:
            return out
        job_id = (self._job or {}).get("id", "job")
        path = self.notes_dir / "handovers" / f"{job_id}-step{i}-try{attempt}-{agent}.md"   # unique per job
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(out + "\n")
        return (out[:HANDOFF_LIMIT] + f"\n[… middle omitted; the whole handover is in {path} …]\n"
                + out[-HANDOFF_LIMIT:])

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
        c.decide = lambda params, agent=base(name): self.decide_permission(agent, params)
        resume_id = self._worker_ids().get(name)
        try:
            await asyncio.wait_for(c.start(resume_id), START_TIMEOUT)
        except BaseException:
            await c.close(grace=0)
            raise
        if resume_id and c.resumed:
            await self.ui.say(f"_{base(name).title()} picked its earlier conversation back up._\n\n")
        self.children[name] = c
        self._save_worker_id(name, c.session_id)
        return c

    def _worker_ids(self):
        """Each worker's own conversation id in this Claudex session, so a resume can reload them."""
        try:
            return json.loads((self.notes_dir / "workers.json").read_text())
        except Exception:
            return {}

    def _save_worker_id(self, name, sid):
        if self.dry_run or not sid:
            return
        try:
            ids = self._worker_ids()
            ids[name] = sid
            self.notes_dir.mkdir(parents=True, exist_ok=True)
            (self.notes_dir / "workers.json").write_text(json.dumps(ids))
        except Exception:
            pass

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
        elif self._task and not self._task.done() and asyncio.current_task() is not self._task:
            self._task.cancel()   # from outside (Ctrl-C, the UI): interrupt whatever it's waiting on
        # from inside the job itself the flag is enough: it's checked before every next move

    # ---------- the lead ----------
    # ---------- permissions ----------
    async def decide_permission(self, agent, params):
        """A worker wants to do something. Claudex's rules, then the gatekeeper, then you (or "no" if you're away)."""
        rules = self._rules or load_rules(self.rules_path, self.project_root, self.models_file)
        tc = params.get("toolCall") or {}
        opts = params.get("options") or []
        kind, command, paths, title = permissions.describe(tc, self.project_root)
        what = permissions.short(" ".join((command or ", ".join(paths) or title or kind).split()), self.project_root)
        log = self.root / "permissions.jsonl"

        def answer(decision, by, why):
            permissions.log_decision(log, agent=agent, kind=kind, what=what[:300], decision=decision, by=by,
                                     why=why[:300], mode=self.mode, session=self.session_key)
            if decision != "allow":     # kept so the worker can be told why, if saying no ends its turn
                self.refusals.append({"agent": agent, "what": what[:300], "why": why[:300], "by": by})
            return permissions.pick_option(opts, decision)

        async def human(why):
            if self.unattended:
                await self.ui.say(f"\n_🛡 Needed your OK, but nobody's here to ask, so it was refused: "
                                  f"{agent.title()} wanted to {kind} {what[:120]} ({why})._\n")
                return answer("deny", "nobody to ask", why)
            if not self._human_waits:
                self._human_since = time.monotonic()
            self._human_waits += 1
            try:
                choice = await self.ui.permission(params)
            finally:
                self._human_waits -= 1
                if not self._human_waits:
                    self._human_elapsed += time.monotonic() - self._human_since
                    self._human_since = None
            picked = ((choice or {}).get("outcome") or {}).get("optionId")
            kind_of = next((o.get("kind", "") for o in opts if o.get("optionId") == picked), "")
            permissions.log_decision(log, agent=agent, kind=kind, what=what[:300],
                                     decision="allow" if kind_of.startswith("allow") else "deny", by="you",
                                     why=why[:300], mode=self.mode, session=self.session_key)
            return choice

        if self.mode == "unrestricted":
            # Some connectors still emit permission requests in their permissive mode.
            # Honour the selected mode here too, using a one-request approval.
            return answer("allow", "unrestricted", "you selected unrestricted mode") or {
                "outcome": {"outcome": "cancelled"}}
        if self.mode == "ask":
            return await human("you chose to approve everything yourself")
        in_force = [self.rules_path or str(rules_file()), self.models_file, rules.get("_models_file")]
        decision, pattern = permissions.rule_decision(rules, kind, command, paths, self.project_root, in_force)
        if decision in ("allow", "deny"):
            if decision == "deny":
                await self.ui.say(f"\n_🛡 Claudex refused: {agent.title()} wanted to {kind} {what[:120]} "
                                  f"(rule `{decision}: {pattern}`)._\n")
            return answer(decision, "rule", pattern) or await human("the agent offered no way to say yes")
        if decision == "ask":
            return await human(f"rule `ask: {pattern}`")
        g = rules.get("permissions", {}).get("gatekeeper") or {}
        if not g:
            return await human("no rule covers it")
        # The same action in the same job gets the same answer: ask the gatekeeper once, not every time.
        seen = (self._job or {}).setdefault("gate", {}) if self._job is not None else {}
        key = (agent, kind, command or "", tuple(paths), "" if command or paths else title or "")
        v = seen.get(key)
        if v is None:
            prompt = permissions.gate_prompt(kind, command, paths, title, (self._job or {}).get("request", ""),
                                             "\n".join(permissions.summary_lines(rules)), self.project_root)
            try:
                _, _, _, v = await self._ask(g.get("agent", "claude"), g.get("model", "sonnet"),
                                             g.get("effort", "low"), prompt, permissions.GATE_SCHEMA,
                                             rules, 120, read_files=False, role="gatekeeper")
            except Exception as e:
                return await human(f"the gatekeeper couldn't decide ({str(e)[:80]})")
            if v.get("decision") in ("allow", "deny"):
                seen[key] = v
        if v["decision"] == "ask":
            return await human(f"gatekeeper: {v['reason']}")
        mark = "allowed" if v["decision"] == "allow" else "refused"
        await self.ui.say(f"\n_🛡 Gatekeeper {mark}: {agent.title()} wants to {kind} {what[:120]}. {v['reason']}_\n")
        return answer(v["decision"], "gatekeeper", v["reason"]) or await human("the agent offered no way to say yes")

    def _work_clock(self):
        now = time.monotonic()
        waiting = now - self._human_since if self._human_since is not None else 0
        return now - self._human_elapsed - waiting

    async def _wait_working(self, awaitable, budget):
        """Bound worker time, excluding human waits; always clean up on stop or timeout."""
        task = asyncio.ensure_future(awaitable)
        started = self._work_clock()
        try:
            while True:
                remaining = budget - (self._work_clock() - started)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                done, _ = await asyncio.wait({task}, timeout=min(remaining, 0.25))
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ---------- usage limits ----------
    def _mark_limited(self, agent, said, resets_at=None):
        agent = base(agent)
        hint = reset_hint(said)
        if resets_at:
            try:
                hint = datetime.datetime.fromtimestamp(float(resets_at)).strftime("at %H:%M on %a %d %b")
            except Exception:
                pass
        self.limited[agent] = (time.time(), hint)
        self._act(agent, "limited")
        self._log(kind="usage_limit", agent=agent, said=(said or "")[:300])

    def _is_limited(self, agent, rules=None):
        hit = self.limited.get(base(agent))
        wait = ((rules or {}).get("limits", {}).get("usage_limit_wait_min", 30)) * 60
        return bool(hit) and time.time() - hit[0] < wait

    def _limit_note(self, agent):
        hint = (self.limited.get(base(agent)) or (0, None))[1]
        return f"{agent.title()} is at its usage limit" + (f" (it says it resets {hint})" if hint else "")

    def _limits_summary(self):
        return "; ".join(self._limit_note(a) for a in self.limited) or "no agent available"

    async def _ask(self, agent, model, effort, prompt, schema, rules, timeout, read_files=True, role=None):
        """A read-only call to one agent. If it's at its usage limit, the other agent's strongest checker
        answers instead. `prompt` may be a function of the agent that answers. Returns (agent, model, effort,
        answer). Other errors are raised as they are."""
        alt = other(agent)
        fb = rules["reviewers"][alt]
        for a, m, e in ((agent, model, effort), (alt, fb["model"], fb["effort"])):
            if self._is_limited(a, rules):
                continue
            try:
                m = project_settings.allowed_model(rules, a, m)
                text = prompt(a) if callable(prompt) else prompt   # built for whoever actually answers
                return a, m, e, await headless.ask(a, self.cwd, text, schema, m, e, timeout, read_files, role=role)
            except Exception as ex:
                limit = getattr(ex, "limit", None)
                if not (limit if limit is not None else error_is_limit(str(ex))):
                    raise
                self._mark_limited(a, str(ex))
                await self.ui.say(f"\n_{self._limit_note(a)}, so {other(a).title()} is taking this one._\n\n")
        raise headless.HeadlessError(f"both agents are at their usage limits: {self._limits_summary()}")

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
        for agent, enabled in rules.get("_enabled_models", {}).items():
            models[agent] = enabled
        files = self._files()
        limited = [a for a in self.limited if self._is_limited(a, rules)]

        def prompt(agent):
            p = (
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
                f"{self._memory_block(agent=agent)}\n\n"
                f"ROUTING RULES (prefer these; explain any deviation in 'why'):\n{json.dumps(rules['rule'], indent=1)}\n"
                f"DEFAULT when no rule clearly fits: {json.dumps(rules['defaults'])}\n"
                f"MODELS EACH AGENT OFFERS NOW: {json.dumps(models)}\n"
                f"Only {rules['browser']['signed_in']} can use the user's signed-in browser.\n"
                + (f"THE USER'S OWN NOTES ON WHO DOES WHAT (follow them):\n{rules['_lead_notes']}\n"
                   if rules.get("_lead_notes") else "")
                + f"\nPROJECT FILES ({len(files)}):\n" + "\n".join(files[:300]) + "\n\n"
                + (f"AT THEIR USAGE LIMIT RIGHT NOW (don't give them steps): {', '.join(limited)}\n" if limited else "")
                + f"USER MESSAGE:\n{text}\n\n"
                "Write each step's instructions so the worker can act without seeing this message.\n")
            if critique:
                p += ("\nA SECOND OPINION on your earlier plan raised these concerns. Revise the plan where they "
                      "are right, and answer EVERY concern in concern_responses (accepted true/false and why):\n"
                      f"{json.dumps(critique, indent=1)}\n")
            else:
                p += "\nLeave concern_responses empty.\n"
            return p
        agent, model, effort = self._lead(rules)
        timeout = rules.get("limits", {}).get("plan_timeout_min", 10) * 60
        a, _, _, answer = await self._ask(agent, model, effort, prompt, PLAN_SCHEMA, rules, timeout, role="lead")
        self._planned_by = a
        return answer

    def _single(self, text, agent, rules, why):
        d = rules["agent_defaults"][agent]
        # Sent straight to Claude (@claude, "Claude only", a Claude skill): it gets the browser, as Claude
        # always has, since nobody planned whether it needs it.
        browser = agent in rules["browser"]["signed_in"]
        return {"steps": [{"title": "Your request", "agent": agent, "model": d["model"], "effort": d["effort"],
                           "browser": browser, "why": why, "instructions": text, "locked": True}],
                "big_decisions": [], "concern_responses": []}

    async def second_opinion(self, text, plan, rules):
        planner = self._planned_by or self._lead(rules)[0]
        who = other(planner)
        r = rules["reviewers"][who]
        def prompt(agent):
            return (
                "You are giving a second opinion on a plan another AI lead wrote, before any work starts. You may "
                "read the project files (read-only). Judge ONLY the big decisions: are they right for this codebase? "
                "Agree unless there is a concrete problem; don't nitpick wording or step order.\n\n"
                f"{self._memory_block(self.CHECK_MEMORY, agent)}\n\n"
                f"USER MESSAGE:\n{text}\n\nPLAN:\n{json.dumps(plan, indent=1)}\n")
        timeout = rules.get("limits", {}).get("check_timeout_min", 15) * 60
        a, m, e, op = await self._ask(who, r["model"], r["effort"], prompt, OPINION_SCHEMA, rules, timeout,
                                      role="second opinion")
        return a, {"model": m, "effort": e, "independent": a != planner}, op

    # ---------- one message ----------
    async def turn(self, text):
        self.cancelled, self.ok = False, True
        self.refusals = []
        self._task = asyncio.current_task()
        rules = load_rules(self.rules_path, self.project_root, self.models_file)
        self._rules = rules
        self._prepare_memory()
        if not self.dry_run:
            usage.remember_project(self.project_root)   # so `claudex usage` can show who did what, and why
        work_lock = None if self.dry_run else Lock(self.root / "work.lock")
        try:
            if work_lock:
                await work_lock.acquire(lambda: self.ui.say(
                    "_Another Claudex job is working in this project; waiting for it to finish…_\n\n"))
            now = datetime.datetime.now()
            self._job = {"at": now.strftime("%Y-%m-%d %H:%M"), "request": text, "rows": [], "gate": {},
                         "id": now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
                         "notes_at": self.notes.stat().st_size if self.notes.exists() else 0}
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
            self._act("lead", "thinking", model=model, effort=effort)
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
            await self.ui.say(f"**Stopped before any work:** couldn't take a snapshot of the project files "
                              f"({gitsnap.LAST_ERROR or 'git failed'}). {SNAP_HELP}\n")
            return "end_turn"
        changed_any = False
        for i, s in enumerate(steps, 1):
            if self.cancelled:
                return "cancelled"
            entries[i - 1]["status"] = "in_progress"
            await self.ui.plan(entries)
            try:
                try:
                    changed = await self._step_or_handover(i, len(steps), s, text, rules)
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

    def _handover_step(self, s, rules, why, said=""):
        to = other(s["agent"])
        d = rules["agent_defaults"][to]
        recap = (f"\n\nHANDOVER: {s['agent'].title()} started this step but {why}. What it said:\n{tail(said, 3000)}\n"
                 "Some of its changes may already be in the files: check `git status` and `git diff` first, "
                 "keep what's right, and finish the step." if said else "")
        return dict(s, agent=to, model=d["model"], effort=s["effort"] if s["effort"] != "low" else d["effort"],
                    why=f"{s['why']} (handed to {to.title()}: {why})", instructions=s["instructions"] + recap)

    async def _step_or_handover(self, i, n, s, text, rules):
        """Run a step; if its agent is (or becomes) at its usage limit, the other agent finishes it."""
        if self._is_limited(s["agent"], rules):
            if self._can_switch(s, rules):
                await self.ui.say(f"_{self._limit_note(s['agent'])}, so {other(s['agent']).title()} takes step {i}._\n\n")
                s = self._handover_step(s, rules, "is at its usage limit")
            else:
                raise StepFailed(self._no_switch_reason(s, rules))
        try:
            return await self._run_step(i, n, s, text, rules)
        except WorkerLimited as w:
            if not self._can_switch(s, rules):
                raise StepFailed(self._no_switch_reason(s, rules))
            await self.ui.say(f"\n_{self._limit_note(w.agent)}. Handing step {i} to "
                              f"{other(w.agent).title()} with a recap of what it did._\n\n")
            s2 = self._handover_step(s, rules, "hit its usage limit", w.said)
            try:
                return await self._run_step(i, n, s2, text, rules, before=w.before)
            except WorkerLimited:
                raise StepFailed(f"both agents are at their usage limits: {self._limits_summary()}. "
                                 "Your work so far is saved; ask again once one of them resets.")

    def _can_switch(self, s, rules):
        if s.get("locked"):        # you chose this agent: never hand it to the other one behind your back
            return False
        to = other(s["agent"])
        if self._is_limited(to, rules):
            return False
        return not (s["browser"] and to not in rules["browser"]["signed_in"])

    def _no_switch_reason(self, s, rules):
        to = other(s["agent"])
        if s.get("locked"):
            return (f"{self._limit_note(s['agent'])}. You asked for {s['agent'].title()} only, so Claudex won't hand "
                    f"this to {to.title()}. Try again once it resets, or send it to {to.title()} yourself.")
        if s["browser"] and to not in rules["browser"]["signed_in"]:
            return (f"{self._limit_note(s['agent'])}, and this step needs your signed-in Chrome, which only "
                    f"{s['agent'].title()} can use. Try again once it resets.")
        return f"both agents are at their usage limits: {self._limits_summary()}. Try again once one resets."

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
        if not r.get("independent", True):
            await self.ui.say(f"**⚠ Not an independent second opinion:** {who.title()} also wrote the plan, because "
                              f"the other side is at its usage limit.\n\n")
        await self.ui.say(f"_Second opinion from {who.title()} {r['model']} ({r['effort']}): "
                          + (f"{fun.pick(fun.AGREE)}_\n\n" if op["verdict"] == "agree" else "some concerns._\n"))
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        record = f"\n## {stamp} · {text[:120]}\n" + "".join(
            f"- **Decision:** {d['decision']}. Why: {d['why']}. Alternatives: {d['alternatives']}\n" for d in decisions)
        record += (f"- **Second opinion ({who} {r['model']} {r['effort']}):** {op['verdict']}"
                   + ("" if r.get("independent", True) else " (NOT independent: the other side was at its usage limit)")
                   + "\n")
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

    def _brief(self, i, n, s, text, c=None):
        memory = self._worker_memory(c) if c is not None else self._memory_block(agent=s["agent"])
        shapes = ("Do only this step. Check your own work where you can (run the tests). Do not commit unless "
                  "asked. The other agent will review your changes afterwards.\n"
                  + QUESTIONS_SHAPE + "\n"
                  f"If you hit a hard-to-undo decision the plan didn't settle (database schema, new dependency, "
                  f"sign-in, money logic, deleting data), stop and write a line starting '{DECISION_TAG}' explaining "
                  "the choice and options, instead of guessing.\n"
                  + REPLY_SHAPE)
        digest = hashlib.sha1(shapes.encode()).hexdigest()
        if c is not None and c.memory_sent.get("shapes") == digest:
            shapes = ("Do only this step; don't commit unless asked; the other agent reviews it afterwards. "
                      f"Questions, '{DECISION_TAG}' and the reply shape are as you were told earlier in this "
                      "conversation.")
        if c is not None:
            c.memory_sent["shapes"] = digest
        return (
            f"You are one worker in Claudex, a team where Claude Code and Codex take turns on a job.\n\n"
            f"{memory}\n\n"
            f"The user's original message:\n{text}\n\n"
            f"Step {i} of {n}: {s['title']}\n\n{s['instructions']}\n\n"
            + shapes)

    async def _configure(self, c, s, rules, model, effort):
        agent = base(c.name)
        model = project_settings.allowed_model(rules, agent, model)
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

    async def _run_step(self, i, n, s, text, rules, before=None):
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
            before = before or gitsnap.snapshot(self.project_root)   # a handover keeps the step's first snapshot
            if before is None and self.reviews_on:
                raise StepFailed(f"couldn't take a snapshot of the project files ({gitsnap.LAST_ERROR or 'git failed'}), "
                                 f"so the work couldn't be checked. {SNAP_HELP}")
            message = self._brief(i, n, s, text, c)
            asked = []     # what the last review asked for, so the re-check confirms each one
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
                self._act(agent, "working", model=model, effort=effort)
                self.active = c
                try:
                    stop, out = await self._wait_working(self._prompt_past_refusals(c, agent, message), step_timeout)
                    needs = getattr(self.ui, "needs_action", None)
                    if needs:
                        for item in human_actions(out):
                            needs(item.strip())
                except asyncio.TimeoutError:
                    c.cancel()
                    await self._stop_child(name)  # make sure it has really stopped before moving on
                    raise StepFailed(f"{agent.title()} ran past the {step_timeout // 60}-minute limit.")
                except asyncio.CancelledError:
                    await self._stop_child(name)
                    raise
                except Exception as e:
                    partial = "".join(getattr(c, "_text", []) or []).strip()   # what it had said before failing
                    await self._stop_child(name)
                    data = getattr(e, "data", None)
                    if error_is_limit(str(e)) or (isinstance(data, dict) and data.get("errorKind") == "rate_limit"):
                        said = (partial + "\n\n" if partial else "") + f"[stopped: {e}]"
                        self._append(self.notes, f"## Step {i}: {s['title']} ({agent} {model} {effort}, attempt "
                                                 f"{attempt}): stopped at its usage limit\n{said}\n\n")
                        self._log(kind="step", step=i, title=s["title"], agent=agent, model=model, effort=effort,
                                  attempt=attempt, stop="error", verdict="usage_limit",
                                  handoff=self._keep_handover(i, attempt, agent, said))
                        self._mark_limited(agent, str(e))
                        raise WorkerLimited(agent, said, before)
                    raise StepFailed(f"{agent.title()} stopped with an error: {e}")
                finally:
                    self.active = None
                secs = round(asyncio.get_running_loop().time() - started, 1)
                await self.ui.say("\n\n")
                self._append(self.notes, f"## Step {i}: {s['title']} ({agent} {model} {effort}, attempt {attempt})"
                                         f"\n{out}\n\n")
                row = dict(kind="step", step=i, title=s["title"], agent=agent, model=model, effort=effort,
                           attempt=attempt, secs=secs, stop=stop, browser=s["browser"], why=s.get("why", "")[:240],
                           handoff=self._keep_handover(i, attempt, agent, out))
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
                    raise StepFailed(f"couldn't take a snapshot of the project files after the step "
                                     f"({gitsnap.LAST_ERROR or 'git failed'}). {SNAP_HELP}")
                diff, files, truncated = gitsnap.diff(self.project_root, before, after,
                                                      int(review.get("diff_limit", 60000)))
                if getattr(c, "limit_hit", None) or reply_is_limit(out):   # the agent flagged a usage limit, or said so at the end
                    self._log(**row, verdict="usage_limit", files=files)
                    self._mark_limited(agent, out, (getattr(c, "limit_hit", None) or {}).get("resetsAt"))
                    raise WorkerLimited(agent, out, before)
                if not files:
                    self._log(**row, verdict="no_changes")
                    return False
                if (not self.reviews_on or not review.get("enabled", True)
                        or (review.get("skip_low_effort", False) and effort == "low")):
                    self._log(**row, verdict="not_reviewed", files=files)
                    self._mark(agent, "unchecked", "checks are off")
                    return True
                tier = self._review_tier(files, review)
                try:
                    verdict = await self._review(s, text, out, diff, files, truncated, rules, tier, asked)
                except BaseException:
                    self._mark(agent, "unchecked", "the check didn't finish")
                    raise
                self._log(**row, verdict=verdict["verdict"], issues=verdict["issues"], files=files, review=tier)
                if self.cancelled:
                    self._mark(agent, "unchecked", "stopped")
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
                asked = list(verdict["issues"])
                message = ("The reviewer (the other agent) checked your changes and asks for these fixes:\n"
                           + "".join(f"- {x}\n" for x in verdict["issues"])
                           + "\nFix them, re-check your work, and end with the same short handoff.")
        finally:
            if chrome:
                chrome.release()

    @staticmethod
    def _review_tier(files, review):
        """'light' when every changed file is writing (docs, notes, text), so a quicker, cheaper model checks it;
        'full' for anything else. Code always gets the full check."""
        if not review.get("light_for_docs", True) or not files:
            return "full"
        docs = all(pathlib.PurePosixPath(f).suffix.lower() in DOC_SUFFIXES for f in files)
        return "light" if docs and review.get("light") else "full"

    def _mark(self, agent, state, by):
        f = getattr(self.ui, "check_mark", None)
        if f:
            try:
                f(agent, state, by)
            except Exception:
                pass

    async def _prompt_past_refusals(self, c, agent, message):
        """Prompt a worker. Codex's only way to say no to an action ("No, and tell Codex what to do differently")
        ends its turn as 'cancelled'. When that happens because Claudex said no, not because you pressed stop,
        tell it why and let it carry on another way, instead of stopping the whole job."""
        said = []
        for _ in range(1 + REFUSAL_RETRIES):
            seen = len(self.refusals)
            stop, out = await c.prompt(message)
            said.append(out)
            new = [r for r in self.refusals[seen:] if r["agent"] == agent]
            if stop != "cancelled" or self.cancelled or not new:
                break
            await self.ui.say("\n_(It was told no, so it's carrying on another way.)_\n\n")
            message = ("Claudex said no to " + "; ".join(f"`{r['what']}` ({r['why']})" for r in new) + ". "
                       "That isn't a stop: carry on with your step another way that doesn't need it. If the step "
                       f"truly can't be done without it, say so and start a line with '{DECISION_TAG}'.")
        return stop, "\n\n".join(x for x in said if x)

    async def _review(self, s, text, out, diff, files, truncated, rules, tier="full", previous=None):
        who = other(s["agent"])
        r = rules["reviewers"][who]
        light = (rules.get("review", {}).get("light") or {}).get(who) if tier == "light" else None
        if light:
            r = dict(r, **light)
        self._act(who, "reviewing", s["agent"], r["model"], r["effort"])
        self._mark(s["agent"], "checking", f"{who.title()} {r['model']}")
        await self.ui.say(f"_{fun.pick(fun.REVIEWING)}: {who.title()} {r['model']} ({r['effort']})"
                          f"{', a lighter check as only writing changed' if light else ''}…_ ")
        again = ("THIS IS A RE-CHECK. Last time you asked for these fixes; confirm each one is fixed, and raise "
                 "new issues only if they are real problems:\n" + "".join(f"- {x}\n" for x in previous) + "\n"
                 if previous else "")

        def prompt(agent):
            return (
                "You are reviewing changes another AI agent just made, as a strict senior engineer. You may read "
                "project files (read-only). Say 'fix' only for real problems: bugs, missed requirements, broken "
                "contracts with other parts, security issues, untested risky logic, or claims in the reply or the "
                "files that aren't true. Don't ask for style changes. Each issue must be concrete and actionable. "
                "Keep the summary to one or two sentences.\n\n"
                f"SHARED PROJECT MEMORY (judge against these rules and decisions too):\n"
                f"{self._memory_block(self.CHECK_MEMORY, agent)}\n\n"
                f"USER MESSAGE:\n{text}\n\nTHE STEP IT WAS GIVEN:\n{s['title']}\n{s['instructions']}\n\n"
                + again +
                f"WORKER'S HANDOFF:\n{tail(out, 4000)}\n\n{SNAPSHOT_NOTE}\nFILES CHANGED: {', '.join(files)}\n\n"
                + ("NOTE: the diff below is cut short; read the remaining changed files directly before judging.\n"
                   if truncated else "") + f"DIFF:\n{diff}\n")
        timeout = rules.get("limits", {}).get("check_timeout_min", 15) * 60
        try:
            used, m, ef, v = await self._ask(who, r["model"], r["effort"], prompt, REVIEW_SCHEMA, rules, timeout,
                                              role="reviewer")
            if used != who:
                v = dict(v, summary=f"(Checked by {used.title()} {m} {ef}, the same side that did the work, "
                                    f"because {who.title()} is at its usage limit.) " + v["summary"])
                await self.ui.say(f"**⚠ Not an independent check:** {who.title()} is at its usage limit, so "
                                  f"{used.title()} checked its own side's work. ")
                who, r = used, {"model": m, "effort": ef}
        except Exception as e:
            if self._carry_on(rules):
                self._mark(s["agent"], "unchecked", "the check couldn't run")
                await self.ui.say(f"\n_(Review unavailable: {e}. Carrying on unreviewed, as the rules allow.)_\n\n")
                return {"verdict": "unreviewed", "issues": [], "summary": f"review unavailable: {e}"}
            raise StepFailed(f"the review couldn't run ({e}). The changes are in place but unchecked. "
                             "Run `claudex doctor`.")
        self._act(who, "pass" if v["verdict"] == "pass" else "fix", s["agent"])
        self._mark(s["agent"], "pass" if v["verdict"] == "pass" else "fix", f"{who.title()} {r['model']}")
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
        prompt = lambda agent: (  # noqa: E731
            "You led this job. All steps are done and each was reviewed. Check that the parts fit together and "
            "the user's request is fully met. You may read project files (read-only).\n\n"
            f"SHARED PROJECT MEMORY:\n{self._memory_block(self.CHECK_MEMORY, agent)}\n\n"
            f"USER MESSAGE:\n{text}\n\nPLAN:\n{json.dumps(steps, indent=1)}\n\n"
            f"{SNAPSHOT_NOTE}\nFILES CHANGED: {', '.join(files)}\n"
            + ("NOTE: the diff is cut short; read the remaining files directly.\n" if truncated else "")
            + f"DIFF:\n{diff}\n")
        timeout = rules.get("limits", {}).get("check_timeout_min", 15) * 60
        try:
            v = (await self._ask(agent, model, effort, prompt, FINAL_SCHEMA, rules, timeout, role="final check"))[3]
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
            changes = gitsnap.changed_status(self.project_root, start, end)
        except gitsnap.SnapshotError:
            changes = []
        files = [f for _, f in changes]
        if (rules.get("memory", {}).get("map_when", "structural") == "structural" and self.map.exists()
                and changes and not map_worthy(changes)):
            return   # only edits inside existing files: the layout the map describes hasn't changed
        prompt = (
            "Update the project map: a short guide (under 150 lines, markdown) for AI agents to this codebase: "
            "layout, key files and what they do, data/database, how it runs, tests and deploys. Keep what is "
            "still true, add what this job changed, remove what is no longer true. You may read files.\n\n"
            f"CURRENT MAP:\n{self._read(self.map, 20000)}\n\nTHIS JOB: {text[:2000]}\n"
            f"FILES CHANGED: {', '.join(f'{st} {f}' for st, f in changes) or ', '.join(files)}\n"
            "(A = added, D = deleted, R = renamed, M = changed.)\n\n"
            f"WHAT THE WORKERS SAID THEY DID (keep anything future agents need in the map):\n{self._job_so_far()}\n")
        try:
            v = (await self._ask(m["agent"], m["model"], m["effort"], prompt, MAP_SCHEMA, rules, 600, role="map"))[3]
        except Exception as e:
            await self.ui.say(f"_(Project map not updated: {e}.)_\n")
            return
        tmp = self.map.with_suffix(".tmp")
        tmp.write_text(v["map"].rstrip() + "\n")
        os.replace(tmp, self.map)
