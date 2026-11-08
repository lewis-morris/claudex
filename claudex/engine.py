"""Claudex: one lead plans each job, big decisions get a second opinion, one worker at a time does each
part, the opposite side reviews every change, and everything lands in shared project memory."""
import asyncio
import contextlib
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

from . import fun, gitsnap, headless, permissions, usage, project_settings, trace
from .child import Child
from .ladder import next_rung
from .locks import CHROME_LOCK, Lock
from . import worktrees

HOME = pathlib.Path(__file__).resolve().parent.parent
from .paths import rules_file
CATALOG = pathlib.Path.home() / ".config/agent-of-empires/acp_option_catalog.json"
NOTES_LIMIT = 8000
MEMORY_LIMIT = 6000
JOURNAL_LIMIT = 12000
SAID_LIMIT = 40000           # the user's own messages this conversation: kept whole, newest first if it overflows
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


FOLLOW_UP_WORDS = ("carry on", "continue", "go on", "keep going", "go ahead", "proceed", "do it", "yes", "yep",
                   "yeah", "ya", "y", "ok", "okay", "sure", "sounds good", "agreed", "that's fine", "thats fine",
                   "fine", "perfect", "great", "lovely", "your call", "you decide", "next", "thoughts", "ideas",
                   "go for it", "crack on", "all good", "looks good", "no", "nope", "n")
FOLLOW_UP_FILLER = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "please", "thanks", "thank", "you", "to", "all",
                    "for", "then", "that", "it", "go", "on", "carry", "continue", "sounds", "good", "fine", "great",
                    "is", "keep", "going", "with", "the", "plan", "do", "proceed", "and", "your", "call", "lets",
                    "let's", "everything", "ahead", "perfect", "agreed", "cheers", "mate", "x", "please"}
_ANSWER = r"(?:\d+\s*[a-f]?|[a-f])(?:\s*[\).:]?)"


def is_follow_up(text):
    """A short reply to the last message, rather than a new request: 'carry on', '1', '1A, 2B', 'A is fine yes',
    '???', 'thoughts?'. Anything longer, or that reads like a new ask, goes to the lead as usual."""
    t = " ".join((text or "").split()).lower()
    if not t or len(t) > 80 or "\n" in (text or "").strip():
        return False
    if re.fullmatch(r"[?!.\s]+", t):
        return True
    if re.fullmatch(_ANSWER + r"(?:\s*[,;/&]?\s*(?:and\s+)?" + _ANSWER + r")*(?:\s+(?:please|thanks|is fine|"
                    r"is good|sounds good|for all|to all|all round)\b.*)?[.!]?", t):
        return True
    t = t.strip(" .!?")
    for w in sorted(FOLLOW_UP_WORDS, key=len, reverse=True):
        if t == w:
            return True
        if t.startswith(w + " ") or t.startswith(w + ","):
            rest = re.findall(r"[a-z0-9']+", t[len(w):])
            # only filler may follow ("carry on yes Y to all"): "ok now build the login page" is a new request
            return all(x in FOLLOW_UP_FILLER or re.fullmatch(r"\d+[a-f]?|[a-f]", x) for x in rest)
    return False


QUESTION_START = ("what", "whats", "what's", "why", "how", "who", "whos", "who's", "where", "when", "which", "is",
                  "are", "was", "were", "do", "does", "did", "have", "has", "hasn't", "haven't", "havent", "hasnt",
                  "isn't", "isnt", "aren't", "arent", "didn't", "didnt", "doesn't", "doesnt", "any", "anything",
                  "should")
QUESTION_FILLER = {"so", "ok", "okay", "and", "but", "sorry", "hmm", "right", "also", "then", "um", "erm", "oh"}
# Words that turn a question into a request for work: those still get planned.
QUESTION_ACTS = re.compile(
    r"\b(make|change|fix|add|remove|delete|drop|implement|build|create|update|upgrade|push|deploy|ship|commit|"
    r"merge|install|write|rewrite|rename|refactor|move|restart|reload|set ?up|get rid|turn (?:on|off)|enable|"
    r"disable|replace|redo|undo|revert|migrate|convert|generate|send|post|publish)\b")


def is_question(text):
    """A short, plain question that asks for an answer, not for work: 'what's left to do?', 'why did it stop?',
    'are we done?'. 'Can you…' and anything naming a change ('should we remove…') goes to the lead."""
    t = " ".join((text or "").split()).lower()
    if not t or len(t) > 240 or "\n" in (text or "").strip() or "?" not in t:
        return False
    words = re.findall(r"[a-z']+", t)
    while words and words[0] in QUESTION_FILLER:
        words.pop(0)
    if not words or words[0] not in QUESTION_START:
        return False
    return not QUESTION_ACTS.search(t)


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
    "where. Before asking anything, check what the user has already said in this conversation and the "
    "project's decision records: drop questions that are already answered, and mark repeats '(still open)'.")
REPLY_SHAPE = (
    "Finish the whole step before you reply: your reply ends your turn, so don't stop part-way to report "
    "progress or to say what's still to do (a part-done step is sent straight back to you). Stop early only "
    "for something you can't do yourself, and say what it is.\n"
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


def _answer_title(title):
    """A quick answer's step, not work: "Answer: …", or carrying on from one."""
    t = str(title or "")
    while t.startswith("Carry on: "):
        t = t[len("Carry on: "):]
    return t.startswith("Answer: ")


class StepFailed(Exception):
    pass


def _hours(secs):
    m = int(secs // 60)
    return f"{m // 60} hour{'s' if m // 60 != 1 else ''}" + (f" {m % 60} min" if m % 60 else "") if m >= 60 else f"{m} minutes"


class WorkerIdle(Exception):
    """A worker showed no sign of work for too long: it's stuck, not busy."""


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
        self._work_lock = None    # held while this job changes files in the main folder
        self._wt = None           # the separate copy this conversation's work is in, while another job has the folder
        self._home = None         # (cwd, project_root, workers) of the main folder, while working in a copy
        self._plan_session = None  # (agent, id) of the lead's kept planning conversation, for a re-plan
        self._carried = None      # the stopped step whose unchecked work this step's check also covers
        self.steers = []          # messages you sent mid-job that no worker has been given yet
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
        self.said_file = self.notes_dir / "said.jsonl"   # every message the user sent in this conversation
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
        self.said_file = self.notes_dir / "said.jsonl"
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

    def _trace(self, **row):
        """One event in the project's run trace (`claudex trace`). A dry run leaves no trace."""
        if not self.dry_run:
            trace.write(self.root, job=(self._job or {}).get("id"), session=self.session_key, **row)

    def _log(self, **row):
        if row.get("kind") == "step":
            extra = getattr(self, "_step_extra", None) or {}
            self._trace(ev="step", **{k: v for k, v in row.items() if k not in ("kind", "handoff", "issues", "files")},
                        handoff_chars=len(row.get("handoff") or ""), files_changed=len(row.get("files") or []),
                        issues=len(row.get("issues") or []), **extra)
        elif row.get("kind") in ("second_opinion", "final_check", "usage_limit"):
            self._trace(ev=row["kind"], **{k: v for k, v in row.items() if k != "kind"})
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
        saved = project_settings.load(self.root.parent)   # the main folder, even from a copy
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

    def _said(self):
        """Everything the user has said in this conversation, oldest first, as [(time, text)]. A conversation
        from before said.jsonl existed is read back from its notes."""
        out = []
        if self.said_file.exists() and self.said_file.stat().st_size:
            for line in self.said_file.read_text(errors="replace").splitlines():
                try:
                    r = json.loads(line)
                    out.append((r.get("at", ""), r.get("text", "")))
                except Exception:
                    pass
            return out
        if self.notes.exists():
            text = self.notes.read_text(errors="replace")
            for m in re.finditer(r"^# (\d{4}-\d\d-\d\d \d\d:\d\d) · Request\n(.*?)(?=^## Step \d|^## Stopped at step|"
                                 r"^# \d{4}-\d\d-\d\d \d\d:\d\d · |\Z)", text, re.M | re.S):
                out.append((m.group(1), m.group(2).strip()))
        return out

    def _said_text(self):
        said = self._said()
        if not said:
            return "(nothing yet)"
        lines = [f"[{at}] {t}" for at, t in said]
        kept, size = [], 0
        for line in reversed(lines):              # newest first, so the latest answers always fit
            if size + len(line) > SAID_LIMIT and kept:
                kept.append(f"[… {len(lines) - len(kept)} earlier message(s) are in {self.notes} …]")
                break
            kept.append(line)
            size += len(line) + 2
        return "\n\n".join(reversed(kept))

    def _memory_parts(self, agent=None):
        """The shared memory as (key, title, text), steadiest first so repeated calls share a cached start."""
        return [
            ("instructions", "PROJECT INSTRUCTIONS (CLAUDE.md and AGENTS.md; follow both)", self._instructions(agent)),
            ("map", "PROJECT MAP", self._read(self.map, MEMORY_LIMIT)),
            ("decisions", "PAST DECISIONS", self._read(self.decisions, MEMORY_LIMIT)),
            ("journal", f"RECENT JOBS IN THIS PROJECT (all sessions; older ones are in {self.journal})",
             self._read(self.journal, JOURNAL_LIMIT)),
            ("notes", "THIS SESSION'S EARLIER NOTES", self._notes_before_job()),
            ("said", "WHAT THE USER HAS SAID IN THIS CONVERSATION (oldest first, every message whole). Their answers "
                     "and corrections stand until they change them: never ask again what they've answered here",
             self._said_text()),
            ("job", "THIS JOB SO FAR (every handover and check, never trimmed)", self._job_so_far()),
        ]

    # Who needs what. Old jobs and session chatter help the planner and the workers; a check of a diff
    # needs the rules, the decisions, the map and this job.
    ALL_MEMORY = ("instructions", "map", "decisions", "journal", "notes", "said", "job")
    CHECK_MEMORY = ("instructions", "map", "decisions", "said", "job")
    FINAL_MEMORY = ("instructions", "decisions", "job")   # each step's reviewer already had the whole conversation

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
        out, now, spared = [], {k: v for k, v in sent.items() if k == "shapes"}, 0   # the reply shape is tracked too
        for key, title, text in self._memory_parts(base(c.name)):
            digest = hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()
            now[key] = digest
            if key != "job" and sent.get(key) == digest:
                out.append(f"{title}: unchanged since you were last given it earlier in this conversation.")
                spared += len(text)
            elif key == "said" and isinstance(sent.get("said_upto"), list) and len(text) > sent["said_upto"][0] \
                    and hashlib.sha1(text[:sent["said_upto"][0]].encode("utf-8", "surrogateescape")).hexdigest() \
                    == sent["said_upto"][1]:
                upto = sent["said_upto"][0]      # they only add up: send just the new messages
                out.append(f"{title}: as given earlier in this conversation, plus these new ones:\n{text[upto:].lstrip()}")
                spared += upto
            else:
                out.append(f"{title}:\n{text}")
            if key == "said":
                now["said_upto"] = [len(text), digest]
        c.memory_sent, c.memory_spared = now, spared
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
        resume_id = None if self._home else self._worker_ids().get(name)   # a copy's workers start fresh there
        try:
            await asyncio.wait_for(c.start(resume_id), START_TIMEOUT)
        except BaseException:
            await c.close(grace=0)
            raise
        if resume_id and c.resumed:
            await self.ui.say(f"_{base(name).title()} picked its earlier conversation back up._\n\n")
        self.children[name] = c
        if not self._home:
            self._save_worker_id(name, c.session_id)
        return c

    async def _fresh_if_full(self, name, c, rules):
        """A worker whose conversation is past `fresh_worker_at` full (70% by default) is swapped for a fresh
        one before its next step: a nearly full conversation gets squashed mid-job and forgets things. The
        fresh one is given the whole memory, including everything the user has said."""
        at = float(rules.get("limits", {}).get("fresh_worker_at", 0.7) or 0)
        u = getattr(c, "usage", None) or {}
        used, size = u.get("used"), u.get("size")
        if not at or not isinstance(used, (int, float)) or not isinstance(size, (int, float)) or not size \
                or used / size < at:
            return c
        await self.ui.say(f"_{base(name).title()}'s conversation is {used / size:.0%} full, so a fresh one takes "
                          "this step with the full memory._\n\n")
        self._trace(ev="fresh_worker", agent=base(name), full=round(used / size, 2))
        await self._stop_child(name)
        self._forget_worker_id(name)
        return await self.child(name, rules)

    def _forget_worker_id(self, name):
        try:
            ids = self._worker_ids()
            if ids.pop(name, None) is not None:
                (self.notes_dir / "workers.json").write_text(json.dumps(ids))
        except Exception:
            pass

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
        for c in ((self._wt or {}).get("children") or {}).values():
            if c not in self.children.values():
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
        rules = self._rules or load_rules(self.rules_path, self.root.parent, self.models_file)
        tc = params.get("toolCall") or {}
        opts = params.get("options") or []
        kind, command, paths, title = permissions.describe(tc, self.project_root)
        what = permissions.short(" ".join((command or ", ".join(paths) or title or kind).split()), self.project_root)
        log = self.root / "permissions.jsonl"

        asked_at = time.monotonic()

        def answer(decision, by, why):
            self._trace(ev="gate", agent=agent, kind=kind, what=what[:120], decision=decision, by=by,
                        secs=round(time.monotonic() - asked_at, 2))
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
            self._trace(ev="gate", agent=agent, kind=kind, what=what[:120], by="you",
                        decision="allow" if kind_of.startswith("allow") else "deny",
                        secs=round(time.monotonic() - asked_at, 2))
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
        decision, pattern = permissions.rule_decision(rules, kind, command, paths, self.project_root, in_force,
                                                      title=title)
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
        remembered = v is not None
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
        return (answer(v["decision"], "gatekeeper (remembered)" if remembered else "gatekeeper", v["reason"])
                or await human("the agent offered no way to say yes"))

    def _work_clock(self):
        now = time.monotonic()
        waiting = now - self._human_since if self._human_since is not None else 0
        return now - self._human_elapsed - waiting

    @staticmethod
    async def _close_cards(c):
        f = getattr(c, "close_cards", None)
        if f:
            try:
                await f()
            except Exception:
                pass

    # ---------- work a stopped step left unchecked ----------
    def _unchecked_path(self):
        return self.notes_dir / "unchecked.json"

    def _keep_unchecked(self, before, title=None):
        """A step was stopped before its changes were checked: remember where it started, so the next step's check
        covers them too. The earliest one is kept if several are stopped in a row."""
        if not before:
            return
        try:
            path = self._unchecked_path()
            if not path.exists():
                path.write_text(json.dumps({"before": before, "title": title or "an earlier step",
                                            "where": str(self.project_root),
                                            "at": datetime.datetime.now().isoformat(timespec="seconds")}))
        except Exception:
            pass

    def _take_unchecked(self, rules):
        """The snapshot a stopped step started from, if what it changed is still there and unchecked."""
        self._carried = None
        try:
            kept = json.loads(self._unchecked_path().read_text())
        except Exception:
            return None
        if not (self.reviews_on and rules.get("review", {}).get("enabled", True)):
            return None
        if kept.get("where") and kept["where"] != str(self.project_root):
            return None       # it's in the other folder (the main one, or a copy): checked when work goes back there
        now = gitsnap.snapshot(self.project_root)
        try:
            if not now or not gitsnap.changed_files(self.project_root, kept["before"], now):
                self._clear_unchecked()
                return None
        except Exception:
            self._clear_unchecked()
            return None
        self._carried = kept.get("title") or "an earlier step"
        self._trace(ev="unchecked_carried", title=self._carried)
        return kept["before"]

    def _clear_unchecked(self):
        self._carried = None
        try:
            self._unchecked_path().unlink()
        except Exception:
            pass

    async def _stopped_unchecked(self, i, s, agent, model, effort, attempt, started, before, c, verdict, why):
        """Log a step stopped by a limit like any other step, so "carry on" goes back to the same worker (which
        reloads its conversation), and keep its changes waiting for a check."""
        self._keep_unchecked(before, s["title"])
        partial = "".join(getattr(c, "_text", []) or []).strip()
        self._append(self.notes, f"## Step {i}: {s['title']} ({agent} {model} {effort}, attempt {attempt}): "
                                 f"stopped\n{partial[-4000:]}\n[{why}]\n\n")
        self._log(kind="step", step=i, title=s["title"], agent=agent, model=model, effort=effort, attempt=attempt,
                  secs=round(asyncio.get_running_loop().time() - started, 1), stop=verdict,
                  browser=s["browser"], why=s.get("why", "")[:240], verdict=verdict,
                  handoff=self._keep_handover(i, attempt, agent, partial + f"\n\n[{why}]"))

    async def _wait_working(self, awaitable, budget, worker=None, idle=None, idle_tool=None):
        """Bound worker time, excluding human waits; always clean up on stop or timeout.

        `budget` is a backstop on the whole step. A worker that's busy (running actions, writing) is left to
        finish; one that shows nothing for `idle` seconds (`idle_tool` while one of its actions is still running,
        since a long build or test run is quiet) is stuck, and WorkerIdle stops it."""
        task = asyncio.ensure_future(awaitable)
        started = self._work_clock()
        began = time.monotonic()
        try:
            while True:
                remaining = budget - (self._work_clock() - started)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                if worker is not None and idle and self._human_since is None:
                    # Never quieter than this step is old: a worker kept from an earlier job last spoke then,
                    # and its new turn may not have started yet.
                    quiet = min(worker.quiet_for() if hasattr(worker, "quiet_for") else 0, time.monotonic() - began)
                    limit = idle_tool if (idle_tool and getattr(worker, "open_cards", None)) else idle
                    if quiet >= limit:
                        raise WorkerIdle(limit)
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

    BUSY_TITLES = {"lead": "Planning", "reviewer": "Checking the change", "second opinion": "Second opinion on the plan",
                   "final check": "Final check", "map": "Updating the project map"}

    @contextlib.asynccontextmanager
    async def _busy(self, role, agent=None, model=None):
        """Show a call that streams nothing (planning, a check) as running work. Agents of Empires cancels a turn
        that shows no progress for two minutes, and a long plan or check is silent until it answers."""
        start = getattr(self.ui, "working", None)
        label = "Waiting for the other Claudex job in this project" if role == "wait" else self.BUSY_TITLES.get(role)
        if not start or not label:
            yield
            return
        tid = await start(label + (f" ({agent.title()} {model})" if agent and model else ""))
        ok = False
        try:
            yield
            ok = True
        finally:
            try:
                await self.ui.done_working(tid, ok)
            except Exception:
                pass

    async def _ask(self, agent, model, effort, prompt, schema, rules, timeout, read_files=True, role=None,
                   keep=False, resume=None):
        """A read-only call to one agent. If it's at its usage limit, the other agent's strongest checker
        answers instead. `prompt` may be a function of the agent that answers. Returns (agent, model, effort,
        answer). Other errors are raised as they are.

        keep=True saves a Claude call's conversation (its id is then in headless.SESSION); resume=(agent, id,
        short prompt) carries that conversation on with the short prompt, when that agent is the one answering."""
        alt = other(agent)
        fb = rules["reviewers"][alt]
        if role == "gatekeeper":      # a yes/no on one action: the other side's light model, not its strongest
            light = (rules.get("review", {}).get("light") or {}).get(alt)
            fb = dict(light, effort="low") if light else fb
        for a, m, e in ((agent, model, effort), (alt, fb["model"], fb["effort"])):
            if self._is_limited(a, rules):
                continue
            try:
                m = project_settings.allowed_model(rules, a, m)
                carry = resume if resume and resume[0] == a else None
                text = carry[2] if carry else prompt(a) if callable(prompt) else prompt   # for whoever answers
                extra = {}
                if a == "claude" and carry:
                    extra["resume"] = carry[1]
                elif a == "claude" and keep:
                    extra["keep"] = True
                began = time.monotonic()
                try:
                    async with self._busy(role, a, m):
                        answer = await headless.ask(a, self.cwd, text, schema, m, e, timeout, read_files, role=role,
                                                    **extra)
                except asyncio.CancelledError:      # stopped, by you or by the host: still worth a line in the trace
                    self._trace(ev="call", role=role, asked=agent, agent=a, model=m, effort=e, ok=False,
                                prompt_chars=len(text), secs=round(time.monotonic() - began, 1), error="cancelled")
                    raise
                except Exception as ex:
                    self._trace(ev="call", role=role, asked=agent, agent=a, model=m, effort=e, ok=False,
                                prompt_chars=len(text), secs=round(time.monotonic() - began, 1),
                                error=str(ex)[:200], **(headless.LAST.get() or {}))
                    raise
                self._trace(ev="call", role=role, asked=agent, agent=a, model=m, effort=e, ok=True,
                            prompt_chars=len(text), secs=round(time.monotonic() - began, 1),
                            **({"resumed": True} if extra.get("resume") else {}),
                            verdict=(answer or {}).get("verdict") or (answer or {}).get("decision"),
                            **(headless.LAST.get() or {}))
                return a, m, e, answer
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
                "The exception is a very big job (a migration, a rebuild across much of the project, anything likely "
                "to take a worker well over half an hour): split it into stages, each one leaving the project "
                "working and small enough to check on its own, so a problem is caught after its stage rather than "
                "at the end. Each stage still goes to the best agent for it (the same one is fine).\n"
                "List in big_decisions ONLY choices that are hard to undo: database schema, a new dependency, "
                "sign-in/permissions, money or VAT logic, a contract between parts, deleting data, architecture. "
                "Usually there are none. Set browser=true only when the step must use the user's signed-in Chrome.\n"
                "You may read project files (read-only), but plan from the project map and memory first and open "
                "only the few files you need to place the work: don't survey the codebase, because each worker "
                "reads what it changes.\n"
                "Before planning any question for the user, check WHAT THE USER HAS SAID, PAST DECISIONS and the "
                "project's own decision records: never ask what they have already answered, and treat their "
                "corrections as settled. When a step depends on something they said earlier, copy their words into "
                "that step's instructions.\n\n"
                f"{self._memory_block(agent=agent)}\n\n"
                f"ROUTING RULES (prefer these; explain any deviation in 'why'):\n{json.dumps(rules['rule'], indent=1)}\n"
                f"DEFAULT when no rule clearly fits: {json.dumps(rules['defaults'])}\n"
                f"MODELS EACH AGENT OFFERS NOW: {json.dumps(models)}\n"
                f"Only {rules['browser']['signed_in']} can use the user's signed-in browser.\n"
                + (f"THE USER'S OWN NOTES ON WHO DOES WHAT (follow them):\n{rules['_lead_notes']}\n"
                   if rules.get("_lead_notes") else "")
                + f"\nPROJECT FILES ({len(files)}):\n" + "\n".join(files[:300]) + "\n\n"
                + (f"AT THEIR USAGE LIMIT RIGHT NOW (don't give them steps): {', '.join(limited)}\n" if limited else "")
                + self._other_job_note()
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
        if critique and not self.lead_effort:   # revising its own plan: the hard thinking is done
            effort = rules["lead"].get("replan_effort") or effort
        timeout = rules.get("limits", {}).get("plan_timeout_min", 10) * 60
        lead = rules.get("lead", {})
        if critique and self._plan_session and lead.get("replan_in_same_conversation", True):
            # The lead carries on the conversation it planned in: it has read the files and knows its plan, so
            # it's sent only the concerns instead of the whole brief again (and doesn't re-read anything).
            short = ("A SECOND OPINION on the plan you just wrote raised these concerns. Revise the plan where they "
                     "are right, and answer EVERY concern in concern_responses (accepted true/false and why). "
                     "Return the whole revised plan in the same shape, every step's instructions in full.\n"
                     + json.dumps({k: v for k, v in critique.items() if k != "earlier_plan"}, indent=1))
            try:
                a, _, _, answer = await self._ask(agent, model, effort, prompt, PLAN_SCHEMA, rules, timeout,
                                                  role="lead", resume=(*self._plan_session, short))
                self._planned_by = a
                return answer
            except headless.HeadlessError as e:
                if getattr(e, "limit", False):
                    raise
                self._trace(ev="replan_fresh", error=str(e)[:200])   # the kept conversation couldn't be resumed
            finally:
                self._forget_plan_session()
        keep = not critique and self.second_on and lead.get("replan_in_same_conversation", True)
        a, _, _, answer = await self._ask(agent, model, effort, prompt, PLAN_SCHEMA, rules, timeout, role="lead",
                                          keep=keep)
        self._planned_by = a
        self._forget_plan_session()
        if keep and a == "claude" and headless.SESSION.get():
            self._plan_session = (a, headless.SESSION.get())
        return answer

    def _forget_plan_session(self):
        if self._plan_session:
            headless.forget(self._plan_session[1])
        self._plan_session = None

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
    async def turn(self, text, already_said=False):
        self.cancelled, self.ok = False, True
        self.refusals = []
        self._task = asyncio.current_task()
        rules = load_rules(self.rules_path, self.project_root, self.models_file)
        self._rules = rules
        self._prepare_memory()
        if not self.dry_run:
            usage.remember_project(self.project_root)   # so `claudex usage` can show who did what, and why
        try:
            now = datetime.datetime.now()
            self._job = {"at": now.strftime("%Y-%m-%d %H:%M"), "request": text, "rows": [], "gate": {},
                         "id": now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
                         "notes_at": self.notes.stat().st_size if self.notes.exists() else 0}
            began, waited_before = time.monotonic(), self._human_elapsed
            self._trace(ev="job_start", request=text[:300], route=self.route, mode=self.mode, checks=self.checks,
                        effort=self.effort)
            ended = lambda outcome, **more: self._trace(  # noqa: E731
                ev="job_end", outcome=outcome, secs=round(time.monotonic() - began, 1),
                waited_for_you=round(self._human_elapsed - waited_before, 1), **more)
            try:
                stop = await self._turn(text, rules, already_said)
                if self._home and self.ok and stop == "end_turn":
                    await self._bring_back(rules)
            except asyncio.CancelledError:
                ended("cancelled")
                raise
            except Exception as e:
                self._trace(ev="error", where="job", error=f"{type(e).__name__}: {e}"[:300])
                ended("failed")
                raise
            ended("done" if self.ok and stop == "end_turn" else "stopped", stop=stop)
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
            self._leave_copy()
            self._release_folder()

    async def _turn(self, text, rules, already_said=False):
        forced = self.route if self.route != "auto" else None
        for tag in ("claude", "codex"):
            if text.lstrip().lower().startswith("@" + tag):
                forced, text = tag, text.lstrip()[len(tag) + 1:].lstrip()
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self._append(self.notes, f"\n# {stamp} · Request\n{text}\n\n")
        if already_said:      # sent mid-job and recorded then; nobody had picked it up, so it's a job of its own now
            pass
        elif not self.said_file.exists() and not self.dry_run:
            # A conversation from before said.jsonl: carry its earlier messages over from the notes first,
            # or starting the file would hide them. (This message is already in the notes, so leave it off.)
            earlier = self._said()[:-1]
            self._append(self.said_file, "".join(json.dumps({"at": at, "text": t}) + "\n" for at, t in earlier))
        if not already_said:
            self._append(self.said_file, json.dumps({"at": stamp, "text": text}) + "\n")

        quick = None if forced else self._follow_up(text, rules)
        if forced:
            plan = self._single(text, forced, rules, f"you chose {forced.title()} only")
        elif quick:
            plan = quick
            s0 = quick["steps"][0]
            if s0.pop("quick", None) == "question":
                s0["answer_only"] = True
                await self.ui.say(f"_A quick question, so {s0['agent'].title()} {s0['model']}, who's already in this "
                                  "conversation, answers it without planning. (Ask for a change and it gets a plan.)_\n\n")
                self._trace(ev="quick_question", agent=s0["agent"], model=s0["model"])
            else:
                await self.ui.say(f"_Short reply, so it goes straight back to {s0['agent'].title()} {s0['model']}, who "
                                  "did the last step, without planning again. (A fuller message gets a fresh plan.)_\n\n")
                self._trace(ev="quick_follow_up", agent=s0["agent"], model=s0["model"])
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
            try:
                plan = await self._check_decisions(text, plan, rules)
            finally:
                self._forget_plan_session()
            if plan is None:
                self.ok = False
                return "end_turn"
            if self.cancelled:
                return "cancelled"

        self._forget_plan_session()       # no second opinion needed it
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

        await self._choose_folder(steps, rules)
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
                    if not s.get("answer_only"):
                        self._clear_unchecked()     # whatever a stopped step left has now been checked
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
                why = self._final_check_needed(rules)
                if why:
                    await self._final_check(text, steps, turn_start, turn_end, rules)
                else:
                    self._trace(ev="final_check_skipped")
                    await self.ui.say("_No final check needed: one agent did every step, and each one passed its "
                                      "review._\n\n")
            if rules.get("memory", {}).get("update_map", True):
                await self._update_map(text, turn_start, turn_end, rules)
        return "end_turn"

    def _follow_up(self, text, rules):
        """A one-step plan for a short reply ("carry on", "1", "A is fine", "???") to the worker who did the last
        step in this conversation: it has the whole context already, so planning again only costs time. None
        when the message isn't a short reply, or there's no recent step to go back to."""
        lead = rules.get("lead", {})
        reply = lead.get("quick_followups", True) and is_follow_up(text)
        question = not reply and lead.get("quick_questions", True) and is_question(text)
        if not (reply or question):
            return None
        last = work = None
        try:
            for line in self.log.read_text(errors="replace").splitlines()[-200:]:
                r = json.loads(line)
                if r.get("kind") == "step" and r.get("agent") in ("claude", "codex") and r.get("model"):
                    last = r
                    if not _answer_title(r.get("title")):
                        work = r
        except Exception:
            return None
        if reply and work and last is not work and work.get("verdict") not in ("pass", "no_changes", "unreviewed"):
            # "carry on" after a quick answer means the work that answer interrupted, if it didn't finish
            last = work
        if not last:
            return None
        try:
            age = time.time() - datetime.datetime.fromisoformat(last["at"]).timestamp()
        except Exception:
            age = 0
        if age > rules.get("lead", {}).get("quick_followup_hours", 12) * 3600:
            return None
        title = str(last.get("title") or "the last step")
        while title.startswith("Carry on: "):
            title = title[len("Carry on: "):]
        if question:
            return {"steps": [{
                "title": ("Answer: " + " ".join(text.split()))[:90], "agent": last["agent"], "model": last["model"],
                "effort": last.get("effort") or rules["agent_defaults"][last["agent"]]["effort"],
                "browser": bool(last.get("browser")), "quick": "question",
                "why": "a short question, so it goes straight to the worker already in this conversation",
                "instructions": (f"The user asks: \u201c{text.strip()}\u201d\n"
                                 "Answer it plainly from this conversation and the project, looking things up if you "
                                 "need to. Don't change any files for this: if the answer is that something should "
                                 "change, say what and ask if they want it done.")}],
                "big_decisions": [], "concern_responses": []}
        return {"steps": [{
            "title": ("Carry on: " + title)[:90], "agent": last["agent"], "model": last["model"],
            "effort": last.get("effort") or rules["agent_defaults"][last["agent"]]["effort"],
            "browser": bool(last.get("browser")),
            "why": "a short reply to the last step, so it goes straight back to the worker who did it",
            "instructions": (f"The user replied to your last message: \u201c{text.strip()}\u201d\n"
                             f"Carry on from where you left off in this conversation (your last step was: {title}). "
                             "If the reply answers questions you asked, apply those answers. If it's a question, "
                             "answer it. If you can't tell what they mean, ask them in one line.")}],
            "big_decisions": [], "concern_responses": []}

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
            + self._copy_note() + shapes)

    # ---------- a separate copy, while another job has the folder ----------
    def _copy_file(self):
        return self.notes_dir / "worktree.json"

    def _copy_note(self):
        if not self._home:
            return ""
        return (f"WHERE YOU WORK: another Claudex job is changing files in the main project folder "
                f"({self._home[1]}), so this job works in a separate copy of it: {self.project_root}. Make every "
                "change in this copy, never in the main folder. Don't commit, push or deploy from the copy: Claudex "
                "brings these changes back into the main folder when the job is done. If the job needs a commit, "
                "push or deploy, say so in your reply.\n\n")

    async def _choose_folder(self, steps, rules):
        """Where this job's steps run. In the main folder when no other job is changing files there (it then
        holds the work lock until the job ends); in a separate copy when one is; and still in the main folder,
        without the lock, when every step only answers. Work a stopped job left in a copy carries on there."""
        if self.dry_run or self._home:
            return
        if self._wt is None:
            try:
                kept = json.loads(self._copy_file().read_text())
                if pathlib.Path(kept["path"]).is_dir():
                    self._wt = kept
                else:
                    self._copy_file().unlink()
            except Exception:
                pass
        if self._wt is not None:
            self._enter_copy()
            await self.ui.say("_Carrying on in this conversation's separate copy of the project._\n\n")
            return
        if all(s.get("answer_only") for s in steps):
            return
        lock = Lock(self.root / "work.lock")
        if lock.try_acquire():
            await self._took_folder(lock, steps)
            return
        par = rules.get("parallel", {})
        stopped_here = False
        try:
            stopped_here = json.loads(self._unchecked_path().read_text()).get("where") == str(self.project_root)
        except Exception:
            pass
        if par.get("worktrees", True) and not stopped_here and worktrees.can_use(self.project_root):
            try:
                wt = await asyncio.to_thread(worktrees.create, self.project_root, self._job["id"],
                                             par.get("dont_link", []), self.root / "work.json")
            except Exception as e:
                self._trace(ev="error", where="copy", error=str(e)[:300])
                await self.ui.say(f"_(Couldn't make a separate copy of the project: {e}.)_\n\n")
            else:
                self._wt = wt
                self._copy_file().write_text(json.dumps(wt))
                self._trace(ev="copy_made", path=wt["path"], linked=len(wt.get("linked", [])))
                self._enter_copy()
                await self.ui.say("_Another Claudex job is changing files in this project, so this one works in a "
                                  "separate copy and brings its changes back when it's done._\n\n"
                                  + ("_(The other job didn't say how it found the folder, so the copy starts "
                                     "from the folder as it is now, part-way through that job's changes.)_\n\n"
                                     if wt.get("from_now") else ""))
                return
        async with self._busy("wait"):
            await lock.acquire(lambda: self.ui.say(
                "_Another Claudex job is changing files in this project; waiting for it to finish…_\n\n"))
        await self._took_folder(lock, steps)

    def _other_job_note(self):
        """What another Claudex job (another conversation) is changing in this project right now, so the plan
        doesn't build the same thing a second time alongside it."""
        if self._work_lock:
            return ""
        try:
            other = json.loads((self.root / "work.json").read_text())
        except Exception:
            return ""
        if not other.get("request") or not Lock(self.root / "work.lock").held_elsewhere():
            return ""
        steps = "".join(f"\n - {t}" for t in other.get("steps") or [] if t)
        return ("ANOTHER CLAUDEX JOB IS WORKING IN THIS PROJECT RIGHT NOW (a different conversation). It was asked:\n"
                f"{other['request']}\nIts steps:{steps or ' (not listed)'}\n"
                "Don't plan work it is already doing: two jobs building the same thing side by side waste the time "
                "and clash when the second brings its changes back. If this message asks for the same work, plan "
                "only what it doesn't cover; if that leaves nothing, answer with one answer_only step that says "
                "the other conversation is already doing it and to ask again here once it has finished.\n\n")

    async def _took_folder(self, lock, steps=()):
        """This job changes the main folder: record how it found it, so a copy made meanwhile starts from there."""
        self._work_lock = lock
        if worktrees.can_use(self.project_root):
            try:
                await asyncio.to_thread(worktrees.record_base, self.project_root, self.root / "work.json",
                                        request=((self._job or {}).get("request") or "")[:600],
                                        steps=[s.get("title", "") for s in steps][:12])
            except Exception as e:
                self._trace(ev="error", where="record_base", error=str(e)[:300])

    def _release_folder(self):
        if self._work_lock:
            try:
                (self.root / "work.json").unlink()
            except Exception:
                pass
            self._work_lock.release()
            self._work_lock = None

    def _enter_copy(self):
        self._home = (self.cwd, self.project_root, self.children)
        self.cwd = self.project_root = self._wt["path"]
        self.children = self._wt.setdefault("children", {})

    def _leave_copy(self):
        if not self._home:
            return
        self.cwd, self.project_root, self.children = self._home
        self._home = None

    async def _bring_back(self, rules):
        """Bring a finished job's changes from its copy into the main folder, then remove the copy. Waits for
        any job changing files in the main folder, so they never land in the middle of its check."""
        if self._unchecked_path().exists():
            return             # a stopped step's work there hasn't been checked yet: it stays in the copy
        wt, home = self._wt, self._home[1]
        lock = Lock(self.root / "work.lock")
        try:
            if not lock.try_acquire():
                async with self._busy("wait"):
                    await lock.acquire(lambda: self.ui.say(
                        "_Bringing this job's changes back once the other Claudex job has finished changing files…_\n\n"))
            brought, clashes, said = await asyncio.to_thread(worktrees.bring_back, home, wt)
        except Exception as e:
            brought, clashes, said = [], [], str(e)
        finally:
            lock.release()
        self._trace(ev="copy_back", files=len(brought), clashes=clashes[:20], said=said[:300])
        if clashes or (said and not brought and said != "nothing to bring back"):
            self.ok = False
            names = ", ".join(f"`{f}`" for f in clashes[:8]) or "some files"
            await self.ui.say(f"\n\n**Couldn't bring this job's changes back:** {names} changed in the main folder "
                              f"since, in the same places. The work is kept in `{wt['path']}` on branch "
                              f"`{wt['branch']}`. Ask for it to be merged in, and the clash sorted out, when the "
                              "other job is done.\n")
            await self._forget_copy(remove=False)
            return
        if brought:
            await self.ui.say(f"\n\n_Brought this job's changes back into the project ({len(brought)} "
                              f"file{'s' if len(brought) != 1 else ''}; {said})._\n")
        await self._forget_copy(remove=True)

    async def _forget_copy(self, remove):
        """Done with the copy: stop its workers, and forget saved conversations of the same agents in the main
        folder, since they don't know this work (a fresh worker gets it from the memory)."""
        wt, home = self._wt, self._home
        agents = list((wt or {}).get("children", {}))
        for c in list((wt or {}).get("children", {}).values()):
            await c.close()
        main_children = home[2] if home else {}
        for name in agents:
            old = main_children.pop(name, None)
            if old:
                await old.close()
            self._forget_worker_id(name)
        if remove and wt:
            try:
                worktrees.remove(home[1] if home else self.project_root, wt)
            except Exception:
                pass
        try:
            self._copy_file().unlink()
        except Exception:
            pass
        if wt is not None:
            wt["children"] = {}
        self._wt = None
        if home:
            self._home = (home[0], home[1], main_children)

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
                c = await self._fresh_if_full(name, c, rules)
            except Exception as e:
                raise StepFailed(f"couldn't start {agent.title()}: {e}")
            model, effort = s["model"], s["effort"]
            review = rules.get("review", {})
            max_attempts = int(review.get("max_attempts", 3))
            # A round in which the worker got further (changed files since the last check, and the checker now
            # asks for something different) doesn't use up an attempt: big steps often come back part-done, and
            # stopping them after three rounds of real progress wastes the work. Up to max_rounds in all.
            max_rounds = max(max_attempts, int(review.get("max_rounds", 8)))
            strikes = 0
            limits = rules.get("limits", {})
            step_timeout = limits.get("step_timeout_min", 180) * 60
            idle = limits.get("step_idle_min", 20) * 60
            idle_tool = limits.get("step_idle_tool_min", 60) * 60
            before = (before or (None if s.get("answer_only") else self._take_unchecked(rules))
                      or gitsnap.snapshot(self.project_root))   # a handover keeps the step's first snapshot
            if before is None and self.reviews_on:
                raise StepFailed(f"couldn't take a snapshot of the project files ({gitsnap.LAST_ERROR or 'git failed'}), "
                                 f"so the work couldn't be checked. {SNAP_HELP}")
            message = self._brief(i, n, s, text, c)
            self._step_extra = {"brief_chars": len(message), "memory_spared": getattr(c, "memory_spared", 0),
                                "steps_in_job": n}
            asked = []     # what the last review asked for, so the re-check confirms each one
            checked = None  # the snapshot the last review saw: a re-check gets only the fixes made since
            for attempt in range(1, max_rounds + 1):
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
                cost_before = (getattr(c, "usage", None) or {}).get("cost")
                for k in ("diff_chars", "diff_truncated", "review_tier", "context_used", "context_size", "cost",
                          "reply_chars", "squashed", "tool_calls", "tool_failures", "failed_actions"):
                    self._step_extra.pop(k, None)      # each attempt's own numbers, never the last one's
                self._step_extra["brief_chars"] = len(message)
                calls_before = (getattr(c, "tool_calls", 0), getattr(c, "tool_failures", 0))
                failed_before = len(getattr(c, "failed", []) or [])
                if attempt > 1:
                    self._step_extra["memory_spared"] = 0
                self._act(agent, "working", model=model, effort=effort)
                self.active = c
                try:
                    stop, out = await self._wait_working(self._prompt_past_refusals(c, agent, message), step_timeout,
                                                         worker=c, idle=idle, idle_tool=idle_tool)
                    needs = getattr(self.ui, "needs_action", None)
                    if needs:
                        for item in human_actions(out):
                            needs(item.strip())
                except (asyncio.TimeoutError, WorkerIdle) as e:
                    stuck = isinstance(e, WorkerIdle)
                    why = (f"{agent.title()} showed no sign of work for {e.args[0] // 60} minutes, so it was stopped "
                           "as stuck" if stuck else
                           f"{agent.title()} was still working after {_hours(step_timeout)}, the limit for one step")
                    c.cancel()
                    await self._close_cards(c)
                    await self._stop_child(name)  # make sure it has really stopped before moving on
                    await self._stopped_unchecked(i, s, agent, model, effort, attempt, started, before, c,
                                                  "stuck" if stuck else "timed_out", why)
                    raise StepFailed(f"{why}. Its conversation is kept: say \u201ccarry on\u201d and it picks up where "
                                     "it stopped, and what it changed is checked then.")
                except asyncio.CancelledError:
                    await self._close_cards(c)
                    await self._stop_child(name)
                    self._keep_unchecked(before)
                    raise
                except Exception as e:
                    partial = "".join(getattr(c, "_text", []) or []).strip()   # what it had said before failing
                    await self._close_cards(c)
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
                u = getattr(c, "usage", None) or {}
                spent = (u["cost"] - (cost_before or 0)) if isinstance(u.get("cost"), (int, float)) else None
                if cost_before is None and getattr(c, "resumed", False):
                    spent = None      # a resumed conversation reports its whole running total, not this step's
                self._step_extra.update(context_used=u.get("used"), context_size=u.get("size"),
                                        cost=round(spent, 4) if spent is not None else None,
                                        reply_chars=len(out or ""), squashed=bool(getattr(c, "squashed", False)),
                                        tool_calls=getattr(c, "tool_calls", 0) - calls_before[0],
                                        tool_failures=getattr(c, "tool_failures", 0) - calls_before[1],
                                        failed_actions=(getattr(c, "failed", []) or [])[failed_before:][:8])
                await self.ui.say("\n\n")
                self._append(self.notes, f"## Step {i}: {s['title']} ({agent} {model} {effort}, attempt {attempt})"
                                         f"\n{out}\n\n")
                row = dict(kind="step", step=i, title=s["title"], agent=agent, model=model, effort=effort,
                           attempt=attempt, secs=secs, stop=stop, browser=s["browser"], why=s.get("why", "")[:240],
                           handoff=self._keep_handover(i, attempt, agent, out))
                if stop == "cancelled" or self.cancelled:
                    await self._close_cards(c)
                    self._keep_unchecked(before)
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
                self._step_extra.update(diff_chars=len(diff), diff_truncated=truncated)
                if getattr(c, "limit_hit", None) or reply_is_limit(out):   # the agent flagged a usage limit, or said so at the end
                    self._log(**row, verdict="usage_limit", files=files)
                    self._mark_limited(agent, out, (getattr(c, "limit_hit", None) or {}).get("resetsAt"))
                    raise WorkerLimited(agent, out, before)
                outside = self._outside(c)
                if outside:
                    self._step_extra["outside"] = outside[:20]
                    await self._warn_outside(outside)
                if files and not self._home and self._work_lock is None and not self.dry_run:
                    # Answering without the folder: what changed may be another job's work in progress.
                    mine = sorted(str(t) for t in (getattr(c, "touched", None) or []))
                    self._log(**row, verdict="not_reviewed" if mine else "no_changes", files=mine[:20])
                    if mine:
                        await self.ui.say(f"\n\n_This answer changed {len(mine)} file(s) while answering, which "
                                          "weren't checked: " + ", ".join(f"`{m}`" for m in mine[:5]) + "._\n")
                    return False
                if not files:
                    self._log(**row, verdict="outside_project" if outside else "no_changes")
                    return False
                if (not self.reviews_on or not review.get("enabled", True)
                        or (review.get("skip_low_effort", False) and effort == "low")):
                    self._log(**row, verdict="not_reviewed", files=files)
                    self._mark(agent, "unchecked", "checks are off")
                    return True
                tier = self._review_tier(files, review)
                self._step_extra["review_tier"] = tier
                fixes_only = False
                progressed = bool(files) if not checked else False
                if asked and checked:
                    try:
                        fdiff, ffiles, fcut = gitsnap.diff(self.project_root, checked, after,
                                                           int(review.get("diff_limit", 60000)))
                    except gitsnap.SnapshotError:
                        ffiles = []
                    progressed = bool(ffiles)
                    if ffiles and review.get("recheck_fixes_only", True):     # the whole step was reviewed last time: send just what changed since
                        diff, truncated, fixes_only = fdiff, fcut, True
                        self._step_extra.update(diff_chars=len(diff), diff_truncated=truncated)
                try:
                    verdict = await self._review(s, text, out, diff, files, truncated, rules, tier, asked,
                                                 fixes_only=fixes_only)
                except BaseException:
                    self._mark(agent, "unchecked", "the check didn't finish")
                    self._keep_unchecked(before, s["title"])      # checked next time instead
                    self._log(**row, verdict="unchecked", files=files)   # so "carry on" goes back to this worker
                    raise
                self._log(**row, verdict=verdict["verdict"], issues=verdict["issues"], files=files, review=tier)
                if self.cancelled:
                    self._mark(agent, "unchecked", "stopped")
                    raise StepFailed("cancelled.")
                if verdict["verdict"] in ("pass", "unreviewed"):
                    return True
                # it got further: it changed files since the last check, and the checker asks for something else now
                got_further = attempt > 1 and progressed and list(verdict["issues"]) != asked
                strikes += 0 if got_further else 1
                if strikes >= max_attempts or attempt == max_rounds:
                    issues = "".join(f"\n- {x}" for x in verdict["issues"])
                    raise StepFailed(f"the review still found problems after {attempt} attempts:{issues}")
                if attempt >= 2:
                    up = next_rung(rules.get("ladder", {}), agent, model, effort)
                    if up:
                        model, effort = up
                        await self.ui.say(f"_Stepping up to {model} {effort}._\n\n")
                asked, checked = list(verdict["issues"]), after
                message = ("The reviewer (the other agent) checked your changes and asks for these fixes:\n"
                           + "".join(f"- {x}\n" for x in verdict["issues"])
                           + "\nFix them, re-check your work, and end with the same short handoff. Keep going until "
                             "all of it is done: don't stop part-way to report progress, because a part-done "
                             "handoff only comes straight back.")
        finally:
            if chrome:
                chrome.release()

    def _outside(self, c):
        """Files the worker's edits named that are outside the folder Claudex watches (and remembers)."""
        root = pathlib.Path(self.project_root).resolve()
        out = []
        for p in sorted(getattr(c, "touched", None) or ()):
            path = pathlib.Path(p if os.path.isabs(p) else os.path.join(self.cwd, p)).resolve()
            if path != root and root not in path.parents:
                out.append(str(path))
        return out

    async def _warn_outside(self, outside):
        """Say it plainly, once per folder per job: work out there isn't checked, and isn't in this memory."""
        seen = (self._job or {}).setdefault("outside_warned", set()) if self._job is not None else set()
        folders = sorted({str(pathlib.Path(p).parent) for p in outside})
        top = repo_root(folders[0]) or folders[0]
        if top in seen:
            return
        seen.add(top)
        await self.ui.say(
            f"\n**⚠ Working outside this project:** the worker edited files in `{top}`, but Claudex is watching "
            f"`{self.project_root}`. Those changes aren't checked by the other agent and aren't in this project's "
            f"memory. For work on that project, start Claudex in `{top}`.\n\n")
        self._append(self.notes, f"## Note: edits outside the project\n{', '.join(outside[:10])}\n\n")

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

    STEER_NOTE = "The user just added this while you were working. Take it into account from here on:\n"

    async def steer(self, text):
        """A message sent while a job is running (Enter mid-turn in Agents of Empires). The worker that's busy gets
        it straight away when it can take one; otherwise the next worker does, and the checks see it too. Returns
        "injected", "held", or "idle" (no job running: send it as a normal message)."""
        text = (text or "").strip()
        if self._job is None or not text:
            return "idle"
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self._append(self.notes, f"\n# {stamp} · Added while working\n{text}\n\n")
        if not self.dry_run:
            self._append(self.said_file, json.dumps({"at": stamp, "text": text}) + "\n")
        self._job.setdefault("steered", []).append(text)
        c = self.active
        if c is not None and getattr(c, "can_steer", False):
            try:
                if await c.steer(self.STEER_NOTE + text) == "injected":
                    await self.ui.say(f"\n_📨 Passed to {base(c.name).title()} straight away._\n\n")
                    self._trace(ev="steer", to="worker", chars=len(text))
                    return "injected"
            except Exception:
                pass
        self.steers.append(text)
        await self.ui.say("\n_📨 Got it. The next worker gets this as soon as it starts, and the check sees it "
                          "too._\n\n")
        self._trace(ev="steer", to="held", chars=len(text))
        return "held"

    def _with_steers(self, message):
        """A worker's prompt, with anything you added mid-job that no worker has had yet."""
        if not self.steers:
            return message
        added, self.steers = "".join(f"- {t}\n" for t in self.steers), []
        return ("THE USER ADDED THIS WHILE THE JOB WAS RUNNING (after the plan was made; take it into account):\n"
                f"{added}\n{message}")

    def _added(self):
        """For checkers: what you added while the job ran."""
        steered = (self._job or {}).get("steered") or []
        return ("ADDED BY THE USER WHILE THE JOB RAN:\n" + "".join(f"- {t}\n" for t in steered) + "\n"
                if steered else "")

    async def _prompt_past_refusals(self, c, agent, message):
        """Prompt a worker. Codex's only way to say no to an action ("No, and tell Codex what to do differently")
        ends its turn as 'cancelled'. When that happens because Claudex said no, not because you pressed stop,
        tell it why and let it carry on another way, instead of stopping the whole job."""
        said = []
        for _ in range(1 + REFUSAL_RETRIES):
            seen = len(self.refusals)
            stop, out = await c.prompt(self._with_steers(message))
            said.append(out)
            new = [r for r in self.refusals[seen:] if r["agent"] == agent]
            if stop != "cancelled" or self.cancelled or not new:
                break
            await self.ui.say("\n_(It was told no, so it's carrying on another way.)_\n\n")
            message = ("Claudex said no to " + "; ".join(f"`{r['what']}` ({r['why']})" for r in new) + ". "
                       "That isn't a stop: carry on with your step another way that doesn't need it. If the step "
                       f"truly can't be done without it, say so and start a line with '{DECISION_TAG}'.")
        return stop, "\n\n".join(x for x in said if x)

    async def _review(self, s, text, out, diff, files, truncated, rules, tier="full", previous=None,
                      fixes_only=False):
        who = other(s["agent"])
        r = rules["reviewers"][who]
        light = (rules.get("review", {}).get("light") or {}).get(who) if tier == "light" else None
        if light:
            r = dict(r, **light)
        small = int(rules.get("review", {}).get("small_diff", 8000))
        if (not light and small and not truncated and len(diff) <= small and len(files) <= 3
                and not previous and r.get("effort") in ("high", "xhigh")):
            # a small change gets the same reviewer model at medium effort; re-checks keep the full effort
            r = dict(r, effort=rules.get("review", {}).get("small_effort", "medium"))
        self._act(who, "reviewing", s["agent"], r["model"], r["effort"])
        self._mark(s["agent"], "checking", f"{who.title()} {r['model']}")
        await self.ui.say(f"_{fun.pick(fun.REVIEWING)}: {who.title()} {r['model']} ({r['effort']})"
                          f"{', a lighter check as only writing changed' if light else ''}…_ ")
        again = ("THIS IS A RE-CHECK. Last time you asked for these fixes; confirm each one is fixed, and raise "
                 "new issues only if they are real problems:\n" + "".join(f"- {x}\n" for x in previous) + "\n"
                 if previous else "")
        if getattr(self, "_carried", None):
            again += (f"This step carries on from one that was stopped before it was checked ({self._carried}). The "
                      "DIFF covers both: review all of it.\n\n")
        if fixes_only:
            again += ("You reviewed the whole step last time. The DIFF below holds only what the worker changed "
                      "since then (its fixes); FILES CHANGED still lists every file the step touched. Open a file "
                      "if you need to see a fix in context.\n\n")

        def prompt(agent):
            return (
                "You are reviewing changes another AI agent just made, as a strict senior engineer. You may read "
                "project files (read-only). Say 'fix' only for real problems: bugs, missed requirements, broken "
                "contracts with other parts, security issues, untested risky logic, or claims in the reply or the "
                "files that aren't true. Don't ask for style changes. Each issue must be concrete and actionable. "
                "Keep the summary to one or two sentences. Start from the diff; open the changed files and the code "
                "that calls or is called by them, and only go wider when something there points you to it.\n\n"
                f"SHARED PROJECT MEMORY (judge against these rules and decisions too):\n"
                f"{self._memory_block(self.CHECK_MEMORY, agent)}\n\n"
                f"USER MESSAGE:\n{text}\n\n{self._added()}THE STEP IT WAS GIVEN:\n{s['title']}\n{s['instructions']}\n\n"
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

    def _final_check_needed(self, rules):
        """Why the final check should run, or None. It looks for parts that don't fit together, which happens
        when different agents did different parts, or when a part wasn't cleanly checked. When one agent did
        every step and each passed its own review, the reviews have already covered it."""
        if rules.get("final_check", {}).get("when", "mixed") == "always":
            return "always"
        rows = [r for r in (self._job or {}).get("rows", []) if r.get("kind") == "step" and r.get("files")]
        if len({r["agent"] for r in rows}) > 1:
            return "more than one agent"
        last = {}
        for r in rows:
            last[r["step"]] = r.get("verdict")
        if any(v != "pass" for v in last.values()):
            return "a step wasn't cleanly reviewed"
        return None

    async def _final_check(self, text, steps, start, end, rules):
        agent, model, effort = self._lead(rules)
        effort = rules.get("final_check", {}).get("effort") or effort   # a fit-together look at reviewed work
        try:
            limit = int(rules.get("final_check", {}).get("diff_limit", 15000))   # each step's diff was reviewed whole
            diff, files, truncated = gitsnap.diff(self.project_root, start, end, limit)
        except gitsnap.SnapshotError as e:
            self.ok = False
            await self.ui.say(f"**Final check couldn't run:** {e}\n")
            return
        self._act("lead", "thinking")
        await self.ui.say(f"_Final check by the lead ({agent.title()} {model})…_ ")
        prompt = lambda agent: (  # noqa: E731
            "You led this job. All steps are done and each one's full diff was already reviewed. Check only that "
            "the parts fit together (names, fields and contracts match across steps) and the user's request is "
            "fully met. You may read project files (read-only); open only what you need for that.\n\n"
            f"SHARED PROJECT MEMORY:\n{self._memory_block(self.FINAL_MEMORY, agent)}\n\n"
            f"USER MESSAGE:\n{text}\n\n{self._added()}PLAN:\n{json.dumps(steps, indent=1)}\n\n"
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
            self._trace(ev="map_skipped", files=len(changes))
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
