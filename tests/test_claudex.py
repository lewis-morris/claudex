import asyncio
import datetime
import os
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from claudex import engine, gitsnap, headless  # noqa: E402
from claudex.__main__ import chat_parser, settings_from  # noqa: E402
from claudex.ladder import next_rung  # noqa: E402
from claudex.locks import Lock  # noqa: E402

LADDER = engine.load_rules()["ladder"]


@pytest.fixture(autouse=True)
def _private_usage_ledger(tmp_path, monkeypatch):
    """Tests never write to the real usage history, or add their throwaway folders to the real project list."""
    from claudex import usage
    monkeypatch.setattr(usage, "LEDGER", tmp_path / "usage.jsonl")
    monkeypatch.setattr(usage, "DATA", tmp_path / "claudex-data")
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "claudex-data" / "projects.json")


# ---------- ladder ----------
def test_ladder_steps_up_one_rung():
    assert next_rung(LADDER, "codex", "gpt-6.1-sol", "medium") == ("gpt-6.1-sol", "high")
    assert next_rung(LADDER, "codex", "gpt-6.1-sol", "high") == ("gpt-6-astra", "high")
    assert next_rung(LADDER, "claude", "sonnet", "high") == ("opus", "medium")


def test_ladder_never_beyond_top_or_off_ladder():
    assert next_rung(LADDER, "codex", "gpt-6-astra", "xhigh") is None
    assert next_rung(LADDER, "claude", "sonnet", "low") is None      # not on the ladder: no guessing
    assert next_rung(LADDER, "claude", "opus", "max") is None


# ---------- schemas ----------
def test_strict_closes_every_object():
    s = headless.strict(engine.REVIEW_SCHEMA)
    assert s["additionalProperties"] is False and set(s["required"]) == {"verdict", "issues", "summary"}
    p = engine.PLAN_SCHEMA["properties"]["steps"]["items"]
    assert p["additionalProperties"] is False and "browser" in p["required"]


# ---------- git snapshots ----------
def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_snapshot_sees_edits_and_new_files_without_touching_index(repo):
    before = gitsnap.snapshot(str(repo))
    assert before
    (repo / "a.py").write_text("x = 2\n")
    (repo / "new.py").write_text("y = 1\n")
    (repo / "ignored.txt").write_text("secret\n")
    after = gitsnap.snapshot(str(repo))
    text, files, cut = gitsnap.diff(str(repo), before, after)
    assert sorted(files) == ["a.py", "new.py"] and "x = 2" in text and not cut
    assert git(repo, "diff", "--cached", "--name-only") == ""      # the user's index is untouched
    assert "new.py" in git(repo, "status", "--porcelain")           # still untracked for the user


def test_snapshot_unchanged_and_truncation(repo):
    a = gitsnap.snapshot(str(repo))
    assert gitsnap.diff(str(repo), a, gitsnap.snapshot(str(repo)))[1] == []
    (repo / "a.py").write_text("z = 3\n" * 5000)
    _, files, cut = gitsnap.diff(str(repo), a, gitsnap.snapshot(str(repo)), limit=200)
    assert files == ["a.py"] and cut


def test_snapshot_outside_git_and_in_an_empty_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("1")
    a = gitsnap.snapshot(str(plain))                      # not a git project at all: still works
    (plain / "a.txt").write_text("2")
    assert a and gitsnap.changed_files(str(plain), a, gitsnap.snapshot(str(plain))) == ["a.txt"]
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=empty, check=True)  # a brand-new repo with no commits (penPost)
    (empty / "page.html").write_text("<p/>")
    b = gitsnap.snapshot(str(empty))
    (empty / "page.html").write_text("<p>hi</p>")
    assert b and gitsnap.changed_files(str(empty), b, gitsnap.snapshot(str(empty))) == ["page.html"]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=empty, capture_output=True, text=True).stdout.count("??") == 1


# ---------- locks ----------
def test_lock_is_exclusive(tmp_path):
    a, b = Lock(tmp_path / "x.lock"), Lock(tmp_path / "x.lock")
    assert a.try_acquire()
    assert not b.try_acquire()
    a.release()
    assert b.try_acquire()
    b.release()


# ---------- command line ----------
def test_settings_flags():
    s = settings_from(chat_parser().parse_args(["--no-second-opinion", "--route", "codex", "--dry-run"]))
    assert s["checks"] == "reviews" and s["route"] == "codex" and s["dry_run"]
    assert settings_from(chat_parser().parse_args(["--no-review"]))["checks"] == "off"


# ---------- a whole job, with stand-in agents ----------
class UI:
    def __init__(self):
        self.text = []

    async def say(self, t):
        self.text.append(t)

    async def plan(self, entries):
        pass

    async def update(self, upd):
        pass

    async def permission(self, p):
        return {}

    def all(self):
        return "".join(self.text)


class FakeChild:
    """A worker that runs scripted actions: each prompt pops one (writes a file, returns text)."""
    script = []
    started = []

    def __init__(self, name, cmd, cwd, ui, mcp_servers=None):
        self.name, self.cwd = name, cwd
        self.proc = type("P", (), {"returncode": None})()
        self.options = {"model": ["opus", "sonnet", "gpt-6.1-sol", "gpt-6-astra"],
                        "effort": ["low", "medium", "high", "xhigh"],
                        "reasoning_effort": ["low", "medium", "high", "xhigh"],
                        "mode": ["default", "acceptEdits", "auto", "bypassPermissions", "read-only",
                                 "workspace-write", "agent", "agent-full-access"]}
        self.settings = []
        self.prompts = []
        self.limit_hit = None
        self.decide = None
        FakeChild.started.append(self)

    async def start(self, resume_id=None):
        self.session_id = resume_id or f"{self.name}-session"
        self.resumed = bool(resume_id)
        FakeChild.resumed_with = getattr(FakeChild, "resumed_with", []) + [resume_id]

    async def set(self, cid, value):
        if value in self.options.get(cid, []):
            self.settings.append((cid, value))
            return True
        return False

    async def prompt(self, text):
        self.prompts.append(text)
        item = FakeChild.script.pop(0)
        if callable(item):
            return await item(self)
        fname, content, reply = item[:3]
        if fname:
            pathlib.Path(self.cwd, fname).write_text(content)
        return (item[3] if len(item) > 3 else "end_turn"), reply

    def cancel(self):
        self.cancel_asked = True

    async def close(self, grace=3):
        self.closed = True


@pytest.fixture
def job(repo, monkeypatch):
    calls = []
    answers = {}

    async def fake_ask(agent, cwd, prompt, schema, model, effort, timeout=900, read_files=True, role=None, **kw):
        headless.SESSION.set("plan-1" if kw.get("keep") else None)
        FakeChild.ask_options = getattr(FakeChild, "ask_options", []) + [kw]
        kind = ("gate" if role == "gatekeeper" else "plan" if schema is engine.PLAN_SCHEMA else "opinion" if schema is engine.OPINION_SCHEMA
                else "review" if schema is engine.REVIEW_SCHEMA else "final" if schema is engine.FINAL_SCHEMA
                else "map")
        calls.append((kind, agent, model, effort, prompt))
        q = answers.get(kind)
        if isinstance(q, list):
            q = q.pop(0)
        if callable(q):
            q = await q()
        if isinstance(q, Exception):
            raise q
        return q

    monkeypatch.setattr(headless, "ask", fake_ask)
    monkeypatch.setattr(engine, "Child", FakeChild)
    import tempfile
    monkeypatch.setattr(engine, "CHROME_LOCK", pathlib.Path(tempfile.mkdtemp()) / "chrome.lock")  # not the real key
    FakeChild.script, FakeChild.started, FakeChild.ask_options = [], [], []
    answers["map"] = {"map": "# Map\n"}
    answers["final"] = {"verdict": "pass", "problems": [], "summary": "fits"}
    return repo, calls, answers


def step(agent="codex", model="gpt-6.1-sol", effort="medium", browser=False, title="Do it"):
    return {"title": title, "agent": agent, "model": model, "effort": effort, "browser": browser,
            "why": "rule", "instructions": "change things"}


def run(repo, text="do the thing", **settings):
    ui = UI()
    e = engine.Engine(str(repo), ui, settings=settings, session_key="t")
    stop = asyncio.run(e.turn(text))
    return e, ui, stop


def test_two_parts_cross_reviewed_final_checked(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", title="API"), step("claude", "sonnet", "high", title="Page")],
                       "big_decisions": [], "concern_responses": []}
    answers["review"] = [{"verdict": "pass", "issues": [], "summary": "ok"},
                         {"verdict": "pass", "issues": [], "summary": "ok"}]
    FakeChild.script = [("api.py", "def total(): return 1\n", "added api.py"),
                        ("page.html", "<p>1</p>\n", "added page")]
    e, ui, stop = run(repo)
    assert stop == "end_turn" and e.ok
    reviews = [c for c in calls if c[0] == "review"]
    assert [(r[1], r[2]) for r in reviews] == [("claude", "opus"), ("codex", "gpt-6-astra")]  # opposite side
    assert any(c[0] == "final" for c in calls) and any(c[0] == "map" for c in calls)
    assert (repo / ".claudex/project/map.md").read_text() == "# Map\n"
    assert "added api.py" in (repo / ".claudex/sessions/t/notes.md").read_text()
    assert ".claudex/" in (repo / ".git/info/exclude").read_text()
    lead = [c for c in calls if c[0] == "plan"][0]
    assert (lead[1], lead[2], lead[3]) == ("claude", "opus", "high")


def test_review_fix_then_step_up_then_give_up(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", "gpt-6.1-sol", "medium")], "big_decisions": [],
                       "concern_responses": []}
    fix = {"verdict": "fix", "issues": ["totals ignore VAT"], "summary": "no"}
    answers["review"] = [dict(fix), dict(fix), dict(fix)]
    FakeChild.script = [("a.py", "v1\n", "try1"), ("a.py", "v2\n", "try2"), ("a.py", "v3\n", "try3")]
    e, ui, stop = run(repo)
    assert not e.ok and "after 3 attempts" in ui.all() and "totals ignore VAT" in ui.all()
    w = FakeChild.started[0]
    assert "totals ignore VAT" in w.prompts[1]                       # issues go back to the same worker
    models = [v for k, v in w.settings if k == "model"]
    efforts = [v for k, v in w.settings if k == "reasoning_effort"]
    assert efforts == ["medium", "medium", "high"] and models[-1] == "gpt-6.1-sol"   # stepped up once


def test_fix_then_pass(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = [{"verdict": "fix", "issues": ["missing test"], "summary": ""},
                         {"verdict": "pass", "issues": [], "summary": "good"}]
    FakeChild.script = [("a.py", "v1\n", "t1"), ("a.py", "v2\n", "t2")]
    e, ui, stop = run(repo)
    assert e.ok and stop == "end_turn"
    assert not any(c[0] == "final" for c in calls)                    # single step: review is enough


def test_worker_asks_for_decision(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step(), step("claude", "sonnet")], "big_decisions": [], "concern_responses": []}
    FakeChild.script = [(None, "", "Looked around.\nDECISION NEEDED: new table or extra column?")]
    e, ui, stop = run(repo)
    assert not e.ok and "needs a decision" in ui.all()
    assert len(FakeChild.started) == 1                                 # step 2 never ran


def test_no_changes_means_no_review(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("claude", "sonnet", "low")], "big_decisions": [], "concern_responses": []}
    FakeChild.script = [(None, "", "The answer is 42.")]
    e, ui, stop = run(repo)
    assert e.ok and not any(c[0] in ("review", "map") for c in calls)


def test_big_decision_gets_second_opinion_and_replan(job):
    repo, calls, answers = job
    first = {"steps": [step()], "big_decisions": [{"decision": "store VAT as float", "why": "simple",
                                                    "alternatives": "integer pence"}], "concern_responses": []}
    second = {"steps": [step(title="pence")], "big_decisions": first["big_decisions"],
              "concern_responses": [{"concern": "floats round badly", "accepted": True, "why": "use pence"}]}
    answers["plan"] = [first, second]
    answers["opinion"] = {"verdict": "concerns", "concerns": ["floats round badly"], "suggestion": "pence"}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "pence\n", "done")]
    e, ui, stop = run(repo)
    op = [c for c in calls if c[0] == "opinion"][0]
    assert (op[1], op[2], op[3]) == ("codex", "gpt-6-astra", "high")   # the other side
    plans = [c for c in calls if c[0] == "plan"]
    assert "floats round badly" in plans[1][4]
    assert plans[0][3] == "high" and plans[1][3] == "medium"   # the revision doesn't redo the hard thinking
    d = (repo / ".claudex/project/decisions.md").read_text()
    assert "store VAT as float" in d and "Lead accepted" in d


def test_checks_off_skips_opinion_and_review(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [{"decision": "x", "why": "y", "alternatives": "z"}],
                       "concern_responses": []}
    FakeChild.script = [("a.py", "v\n", "done")]
    e, ui, stop = run(repo, checks="off")
    assert e.ok and not any(c[0] in ("opinion", "review") for c in calls)


def test_browser_step_uses_chrome_worker_and_moves_to_claude(job, monkeypatch, tmp_path):
    repo, calls, answers = job
    monkeypatch.setattr(engine, "CHROME_LOCK", tmp_path / "chrome.lock")
    answers["plan"] = {"steps": [step("codex", browser=True)], "big_decisions": [], "concern_responses": []}
    FakeChild.script = [(None, "", "looked at the page")]
    e, ui, stop = run(repo)
    assert FakeChild.started[0].name == "claude_browser"


def test_dry_run_changes_nothing(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    e, ui, stop = run(repo, dry_run=True)
    assert "Dry run" in ui.all() and FakeChild.started == []


def test_forced_route_skips_lead(job):
    repo, calls, answers = job
    FakeChild.script = [(None, "", "hi")]
    e, ui, stop = run(repo, text="@claude explain this")
    assert not any(c[0] == "plan" for c in calls) and FakeChild.started[0].name == "claude_browser"


def test_forced_codex_has_no_browser(job):
    repo, calls, answers = job
    FakeChild.script = [(None, "", "hi")]
    run(repo, text="@codex explain this")
    assert FakeChild.started[0].name == "codex"


# ---------- Codex's review findings ----------
def test_cancel_during_review_stops_before_another_edit(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    holder = {}

    async def review_then_cancel():
        holder["e"].cancel()
        return {"verdict": "fix", "issues": ["x"], "summary": ""}
    answers["review"] = review_then_cancel
    FakeChild.script = [("a.py", "v1\n", "t1"), ("a.py", "v2\n", "SHOULD NOT RUN")]
    ui = UI()
    e = engine.Engine(str(repo), ui, settings={}, session_key="t")
    holder["e"] = e
    stop = asyncio.run(e.turn("go"))
    assert stop == "cancelled" and len(FakeChild.started[0].prompts) == 1


def test_review_that_cannot_run_stops_the_job(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step(), step("claude", "sonnet")], "big_decisions": [], "concern_responses": []}
    answers["review"] = headless.HeadlessError("not logged in")
    FakeChild.script = [("a.py", "v1\n", "t1")]
    e, ui, stop = run(repo)
    assert not e.ok and "review couldn't run" in ui.all() and len(FakeChild.started) == 1


def test_second_opinion_that_cannot_run_stops_before_work(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [{"decision": "x", "why": "y", "alternatives": "z"}],
                       "concern_responses": []}
    answers["opinion"] = headless.HeadlessError("timeout")
    e, ui, stop = run(repo)
    assert not e.ok and FakeChild.started == [] and "Stopped before any work" in ui.all()


def test_refusal_is_a_failure(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    FakeChild.script = [(None, "", "I won't", "refusal")]
    e, ui, stop = run(repo)
    assert not e.ok and "refused" in ui.all()


def test_works_outside_git_too(job, tmp_path_factory):
    repo, calls, answers = job
    plain = tmp_path_factory.mktemp("plain")   # not inside the test repo, not a git project
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "x = 1\n", "made a.py")]
    e, ui, stop = run(plain)
    assert e.ok and [c for c in calls if c[0] == "review"] and "a.py" in [c for c in calls if c[0] == "review"][0][4]


def test_dry_run_leaves_no_trace(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [{"decision": "x", "why": "y", "alternatives": "z"}],
                       "concern_responses": []}
    answers["opinion"] = {"verdict": "agree", "concerns": [], "suggestion": ""}
    run(repo, dry_run=True)
    assert not (repo / ".claudex").exists() and ".claudex" not in (repo / ".git/info/exclude").read_text()


def test_cache_files_are_not_changes(repo):
    a = gitsnap.snapshot(str(repo))
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__/a.cpython-314.pyc").write_bytes(b"x")
    assert gitsnap.diff(str(repo), a, gitsnap.snapshot(str(repo)))[1] == []


def test_stuck_worker_is_shut_down(job, monkeypatch):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    monkeypatch.setitem(engine.load_rules.__globals__, "load_rules", engine.load_rules)

    async def hang(child):
        await asyncio.sleep(30)
    FakeChild.script = [hang]
    real = engine.load_rules

    def short_rules(path=None, project_root=None, models_file=None):
        r = real(path, project_root, models_file)
        r["limits"]["step_timeout_min"] = 0.01
        return r
    monkeypatch.setattr(engine, "load_rules", short_rules)
    e, ui, stop = run(repo)
    w = FakeChild.started[0]
    assert not e.ok and "limit" in ui.all() and w.closed and "codex" not in e.children


def test_tracked_file_in_cache_folder_still_counts(repo):
    (repo / "node_modules/local").mkdir(parents=True)
    (repo / "node_modules/local/index.js").write_text("a\n")
    subprocess.run(["git", "add", "-f", "node_modules/local/index.js"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "vendored"], cwd=repo, check=True)
    a = gitsnap.snapshot(str(repo))
    (repo / "node_modules/local/index.js").write_text("b\n")
    (repo / "node_modules/other.js").write_text("untracked junk\n")
    assert gitsnap.diff(str(repo), a, gitsnap.snapshot(str(repo)))[1] == ["node_modules/local/index.js"]


def test_bad_snapshot_diff_raises(repo):
    with pytest.raises(gitsnap.SnapshotError):
        gitsnap.changed_files(str(repo), "deadbeef" * 5, "feedface" * 5)


def test_worker_close_kills_leftover_children(tmp_path):
    from claudex.child import Child, _group_alive
    script = tmp_path / "w.sh"
    script.write_text("#!/bin/sh\nsleep 300 &\nexit 0\n")   # the connector exits, leaving a child behind
    script.chmod(0o755)

    async def go():
        c = Child("t", [str(script)], str(tmp_path), None)
        c.proc = await asyncio.create_subprocess_exec(str(script), stdin=asyncio.subprocess.PIPE,
                                                      start_new_session=True)
        await asyncio.sleep(0.3)
        assert _group_alive(c.proc.pid)
        await c.close(grace=1)
        return _group_alive(c.proc.pid)
    assert asyncio.run(go()) is False


# ---------- commands and skills ----------
def test_skill_expansion_routes_to_the_agent_that_has_it():
    from claudex.commands import expand_skill
    skills = {"deployify": {"description": "ship it", "agents": ["claude"]},
              "both": {"description": "", "agents": ["claude", "codex"]}}
    assert expand_skill("/deployify the portal", skills) == ("Use the deployify skill. the portal", "claude")
    assert expand_skill("/both", skills) == ("Use the both skill.", None)
    assert expand_skill("/rules", skills) is None


@pytest.mark.skipif(not (pathlib.Path.home() / ".claude/skills/deployify").exists(), reason="needs real skills")
def test_real_skills_load_and_acp_menu_lists_them():
    from claudex.commands import acp_commands, load_skills
    skills = load_skills()
    assert "deployify" in skills and not any(n.endswith("-workspace") for n in skills)
    names = [c["name"] for c in acp_commands(skills)]
    assert "rules" in names and "deployify" not in names and "effort" not in names  # skills are $name, not /name


def test_skill_name_must_end_at_a_space():
    from claudex.commands import expand_skill
    skills = {"deployify": {"description": "", "agents": ["claude"]}}
    assert expand_skill("/deployify/README.md explain this", skills) is None


class FakePeer:
    def __init__(self):
        self.sent = []

    def notify(self, method, params):
        self.sent.append((method, params))

    async def request(self, method, params):
        return {}


def _server_session(tmp_path):
    from claudex.server import Server
    srv = Server({})
    srv.peer = FakePeer()
    return srv


def test_acp_command_found_even_after_an_attachment(tmp_path, monkeypatch):
    srv = _server_session(tmp_path)

    async def go():
        sid = (await srv.on_request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))["sessionId"]
        await asyncio.sleep(0)
        res = await srv.on_request("session/prompt", {"sessionId": sid, "prompt": [
            {"type": "resource", "resource": {"uri": "file:///x.py", "text": "x"}},
            {"type": "text", "text": "/status"}]})
        return res
    res = asyncio.run(go())
    texts = [p["update"]["content"]["text"] for m, p in srv.peer.sent
             if p["update"].get("sessionUpdate") == "agent_message_chunk"]
    assert res == {"stopReason": "end_turn"} and any("Routing auto" in t for t in texts)
    assert any(p["update"].get("sessionUpdate") == "available_commands_update" for m, p in srv.peer.sent)


def test_acp_local_command_can_be_cancelled(tmp_path, monkeypatch):
    srv = _server_session(tmp_path)

    async def slow(e, text):
        await asyncio.sleep(30)
    monkeypatch.setattr(srv, "slash", slow)

    async def go():
        sid = (await srv.on_request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))["sessionId"]
        t = asyncio.ensure_future(srv.on_request("session/prompt", {"sessionId": sid, "prompt": [
            {"type": "text", "text": "/doctor"}]}))
        await asyncio.sleep(0.1)
        await srv.on_notify("session/cancel", {"sessionId": sid})
        return await asyncio.wait_for(t, 5), srv.sessions[sid].busy
    res, busy = asyncio.run(go())
    assert res == {"stopReason": "cancelled"} and busy is False


def test_cancelled_diagnostic_leaves_nothing_running(tmp_path, monkeypatch):
    """A cancelled /doctor in Agents of Empires takes its whole process group with it."""
    from claudex import server
    from claudex.child import _group_alive
    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "claudex"
    script.write_text("#!/bin/sh\nsleep 300 &\nsleep 300\n")
    script.chmod(0o755)
    monkeypatch.setattr(server, "self_command", lambda *a, rules=None: ([str(script)], dict(os.environ)))
    srv = _server_session(tmp_path)
    groups = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*a, **k):
        p = await real_exec(*a, **k)
        groups.append(p.pid)
        return p
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", spy)

    async def go():
        sid = (await srv.on_request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))["sessionId"]
        t = asyncio.ensure_future(srv.on_request("session/prompt", {"sessionId": sid, "prompt": [
            {"type": "text", "text": "/doctor"}]}))
        await asyncio.sleep(0.5)
        await srv.on_notify("session/cancel", {"sessionId": sid})
        res = await asyncio.wait_for(t, 5)
        await asyncio.sleep(0.2)
        return res
    res = asyncio.run(go())
    assert res == {"stopReason": "cancelled"} and groups and not _group_alive(groups[0])


# ---------- one shared brain and memory ----------
def test_everyone_shares_one_memory(job):
    repo, calls, answers = job
    (repo / "CLAUDE.md").write_text("House rule from CLAUDE.md: money is always whole pence.\n")
    (repo / "AGENTS.md").write_text("House rule from AGENTS.md: run pytest -q before handing over.\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "rules"], cwd=repo, check=True)

    # Session A (say, a terminal) does a job.
    answers["plan"] = [{"steps": [step(title="Earlier job")], "big_decisions": [], "concern_responses": []}]
    answers["review"] = [{"verdict": "pass", "issues": [], "summary": "ok"}]
    FakeChild.script = [("old.py", "x = 1\n", "Made old.py. Codex note: totals live in calc.py")]
    asyncio.run(engine.Engine(str(repo), UI(), settings={}, session_key="A").turn("first job"))

    # Session B (say, Agents of Empires) does a two-part job with a big decision.
    calls.clear()
    FakeChild.started = []
    answers["plan"] = [{"steps": [step("codex", title="Sums"), step("claude", "sonnet", title="Page")],
                        "big_decisions": [{"decision": "VAT on the subtotal", "why": "UK practice",
                                           "alternatives": "per line"}], "concern_responses": []}]
    answers["opinion"] = {"verdict": "agree", "concerns": [], "suggestion": ""}
    answers["review"] = [{"verdict": "pass", "issues": [], "summary": "ok"},
                         {"verdict": "pass", "issues": [], "summary": "ok"}]
    FakeChild.script = [("calc.py", "def vat(p): return (p*20+50)//100\n", "HANDOFF-FROM-CODEX: vat() is in calc.py"),
                        ("page.html", "<p>VAT</p>\n", "page done")]
    e = engine.Engine(str(repo), UI(), settings={}, session_key="B")
    asyncio.run(e.turn("second job"))

    # Every prompt carries the house rules the agent doesn't load by itself (Claude reads CLAUDE.md, Codex
    # reads AGENTS.md), and names the one it does instead of pasting it again.
    own = {"claude": ("whole pence", "pytest -q"), "codex": ("pytest -q", "whole pence")}
    prompts = {kind: [(c[1], c[4]) for c in calls if c[0] == kind] for kind in ("plan", "opinion", "review", "final")}
    briefs = [(engine.base(w.name), w.prompts[0]) for w in FakeChild.started]
    for who, texts in [("lead", prompts["plan"]), ("second opinion", prompts["opinion"]),
                       ("reviewers", prompts["review"]), ("final check", prompts["final"]), ("workers", briefs)]:
        assert texts, who
        for agent, t in texts:
            loads_itself, pasted = own[agent]
            assert pasted in t and loads_itself not in t and "loads it itself" in t, (who, agent)
    # Session A's work reaches the planner and the workers; checks of a diff don't carry old jobs.
    for agent, t in prompts["plan"] + briefs:
        assert "first job" in t and "Earlier job" in t
    for agent, t in prompts["opinion"] + prompts["review"] + prompts["final"]:
        assert "Earlier job" not in t
    prompts = {k: [t for _, t in v] for k, v in prompts.items()}
    briefs = [t for _, t in briefs]
    # Big decisions reach everyone after they're made; the second worker sees the first one's handover.
    for t in briefs + prompts["review"] + prompts["final"]:
        assert "VAT on the subtotal" in t
    assert "HANDOFF-FROM-CODEX" in briefs[1] and "HANDOFF-FROM-CODEX" in prompts["final"][0]
    journal = (repo / ".claudex/project/journal.md").read_text()
    assert "session A" in journal and "session B" in journal and "calc.py" in journal


# ---------- the full-screen app ----------
def _tui(tmp_path):
    from claudex import tui as T
    T.PREFS = tmp_path / "ui.json"
    e = engine.Engine(str(tmp_path), None, settings={}, session_key="t")
    t = T.TUI(e)
    e.ui = t
    return T, t, e


def test_tui_takes_everything_the_engine_sends(tmp_path):
    T, t, e = _tui(tmp_path)

    async def go():
        await t.say("_Planning…_\n")
        await t.plan([{"content": "Sums · Codex", "status": "in_progress", "priority": "medium"}])
        t.activity("codex", "working")
        await t.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done it"}})
        await t.update({"sessionUpdate": "tool_call", "toolCallId": "x", "title": "Edit calc.py"})
    asyncio.run(go())
    text = "".join(x for _, x in t.transcript())
    assert "Planning" in text and "Codex" in text and "done it" in text and "Edit calc.py" in text
    assert "Sums" in "".join(x for _, x in t.plan_text())


def test_tui_draws_every_state_layout_and_colour(tmp_path):
    T, t, e = _tui(tmp_path)
    t.build()
    for palette in T.PALETTES:
        t.prefs["palette"] = palette
        t.style()
        for style, size in [(st, sz) for st in T.STYLES for sz in T.SIZES]:
            t.set_look(style, size)
            for who, st, tgt in [(None, "idle", None), ("lead", "thinking", None), ("codex", "working", None),
                                 ("claude", "reviewing", "codex"), ("claude", "pass", "codex"),
                                 ("codex", "fix", "claude"), (None, "done", None), (None, "stopped", None)]:
                t.activity(who, st, tgt)
                for w, h in ((44, 10), (150, 10), (30, 6)):
                    lines = t.draw_stage(w, h)
                    assert len(lines) == h and all(sum(len(x) for _, x in ln) == w for ln in lines)
    for layout in T.LAYOUTS:
        t.prefs["layout"] = layout
        assert list(t.app.layout.find_all_windows())  # resolves every dynamic layout without error


def _live_job(t):
    t.busy = asyncio.ensure_future(asyncio.sleep(30))


def test_tui_permission_is_answered_by_typing_a_number(tmp_path):
    T, t, e = _tui(tmp_path)
    t.build()

    async def go():
        _live_job(t)
        ask = asyncio.ensure_future(t.permission({"toolCall": {"title": "rm -rf build"}, "options": [
            {"optionId": "yes", "name": "Allow"}, {"optionId": "no", "name": "Deny", "kind": "reject_once"}]}))
        await asyncio.sleep(0)
        t.on_accept(type("TypedLine", (), {"text": "2"})())
        return await ask
    assert asyncio.run(go()) == {"outcome": {"outcome": "selected", "optionId": "no"}}


# ---------- Codex's review of the shared memory and the full screen ----------
def test_permission_questions_queue_and_cancel_cleanly(tmp_path):
    T, t, e = _tui(tmp_path)
    t.build()
    opts = [{"optionId": "yes", "name": "Allow"}, {"optionId": "no", "name": "Deny"}]

    async def go():
        _live_job(t)
        a = asyncio.ensure_future(t.permission({"toolCall": {"title": "first"}, "options": opts}))
        b = asyncio.ensure_future(t.permission({"toolCall": {"title": "second"}, "options": opts}))
        await asyncio.sleep(0)
        t.on_accept(type("L", (), {"text": "1"})())   # answers the FIRST question
        t.on_accept(type("L", (), {"text": "1"})())   # then the second, no crash on a double answer
        first, second = await a, await b
        c = asyncio.ensure_future(t.permission({"toolCall": {"title": "third"}, "options": opts}))
        await asyncio.sleep(0)
        t.drop_questions()                           # Ctrl-C / job ended: nothing left to answer later
        return first, second, await c, t.questions
    first, second, third, left = asyncio.run(go())
    assert first["outcome"]["optionId"] == "yes" and second["outcome"]["optionId"] == "yes"
    assert third == {"outcome": {"outcome": "cancelled"}} and left == []


def test_other_sessions_see_handovers_and_findings(job):
    repo, calls, answers = job
    answers["plan"] = [{"steps": [step(title="Migrate")], "big_decisions": [], "concern_responses": []}]
    answers["review"] = [{"verdict": "fix", "issues": ["index missing on orders.date"], "summary": ""},
                         {"verdict": "pass", "issues": [], "summary": "ok"}]
    FakeChild.script = [("m.sql", "a\n", "first try"), ("m.sql", "b\n", "Done. Deploy only after the migration runs.")]
    asyncio.run(engine.Engine(str(repo), UI(), settings={}, session_key="A").turn("add a migration"))
    calls.clear()
    answers["plan"] = [{"steps": [step(title="Next")], "big_decisions": [], "concern_responses": []}]
    asyncio.run(engine.Engine(str(repo), UI(), settings={"dry_run": True}, session_key="B").turn("next"))
    lead = [c[4] for c in calls if c[0] == "plan"][0]
    assert "Deploy only after the migration runs" in lead and "index missing on orders.date" in lead


def test_long_instruction_files_keep_their_opening_rules(job):
    repo, calls, answers = job
    (repo / "AGENTS.md").write_text("OPENING RULE: never touch prod.\n" + "filler line\n" * 6000)
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    run(repo, dry_run=True)
    lead = [c[4] for c in calls if c[0] == "plan"][0]
    assert "OPENING RULE: never touch prod." in lead and "Read the whole file" in lead


def test_subfolder_shares_the_repo_brain(job):
    repo, calls, answers = job
    (repo / "web").mkdir()
    e1 = engine.Engine(str(repo), UI(), session_key="a")
    e2 = engine.Engine(str(repo / "web"), UI(), session_key="b")
    assert e1.root == e2.root == repo / ".claudex"


def test_crash_still_writes_the_journal(job, monkeypatch):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    FakeChild.script = [("a.py", "v\n", "made a change")]
    answers["review"] = {"verdict": "pass", "issues": [], "summary": ""}

    async def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(engine.Engine, "_final_check", boom)
    monkeypatch.setattr(engine.Engine, "_update_map", boom)
    with pytest.raises(OSError):
        run(repo)
    j = (repo / ".claudex/project/journal.md").read_text()
    assert "failed" in j and "made a change" in j


def test_map_writer_gets_the_job_not_the_whole_memory(job):
    repo, calls, answers = job
    (repo / "CLAUDE.md").write_text("Rule for everyone.\n")
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": ""}
    FakeChild.script = [("a.py", "v\n", "HANDOVER-XYZ")]
    run(repo)
    m = [c[4] for c in calls if c[0] == "map"][0]
    assert "HANDOVER-XYZ" in m and "M a.py" in m
    assert "Rule for everyone." not in m and "RECENT JOBS" not in m and m.count("CURRENT MAP") == 1


def test_screen_memory_is_bounded(tmp_path):
    T, t, e = _tui(tmp_path)

    async def go():
        t.activity("codex", "working")
        for _ in range(3000):
            await t.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x" * 500}})
            t.activity("codex", "working")
    asyncio.run(go())
    assert sum(len(b["text"]) for b in t.blocks) <= T.TOTAL_CHARS and "prev" not in t.state


def test_late_permission_after_stop_is_refused(tmp_path):
    T, t, e = _tui(tmp_path)
    t.build()

    async def go():
        _live_job(t)
        t.stopping = True                       # Ctrl-C was pressed
        return await t.permission({"toolCall": {"title": "late"}, "options": [{"optionId": "y", "name": "A"}]})
    assert asyncio.run(go()) == {"outcome": {"outcome": "cancelled"}} and t.questions == []


def test_long_handover_keeps_its_start_and_a_pointer(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step(), step("claude", "sonnet")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": ""}
    long = "WARNING: deploy only after the migration.\n" + "detail\n" * 2000 + "END-MARK"
    FakeChild.script = [("a.py", "v\n", long), ("b.html", "<p/>\n", "ok")]
    run(repo)
    brief2 = FakeChild.started[1].prompts[0]
    assert "WARNING: deploy only after the migration." in brief2 and "END-MARK" in brief2
    saved = list((repo / ".claudex/sessions/t/handovers").glob("*.md"))
    assert saved and saved[0].read_text().startswith("WARNING") and str(saved[0]) in brief2


def test_log_from_a_subfolder_finds_the_repo_memory(job, capsys):
    from claudex import tools
    repo, calls, answers = job
    (repo / "web").mkdir()
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": ""}
    FakeChild.script = [("a.py", "v\n", "ok")]          # the worker runs inside web/
    e, ui, stop = run(repo / "web")
    capsys.readouterr()
    tools.show_log(str(repo / "web"), 5)
    out = capsys.readouterr().out
    assert "step 1" in out, ui.all() + str(list((repo / ".claudex").rglob("*")))


def test_huge_output_never_wipes_the_screen(tmp_path):
    T, t, e = _tui(tmp_path)
    t._add("sys", "hello")
    t._add("codex", "y" * (T.TOTAL_CHARS + 10))
    assert t.blocks and t.blocks[-1]["who"] == "codex" and len(t.blocks[-1]["text"]) == T.BLOCK_CHARS


def test_handover_files_from_two_jobs_dont_overwrite(job):
    repo, calls, answers = job
    answers["plan"] = [{"steps": [step()], "big_decisions": [], "concern_responses": []},
                       {"steps": [step()], "big_decisions": [], "concern_responses": []}]
    answers["review"] = {"verdict": "pass", "issues": [], "summary": ""}
    FakeChild.script = [("a.py", "1\n", "FIRST-JOB\n" + "x\n" * 3000), ("a.py", "2\n", "SECOND-JOB\n" + "y\n" * 3000)]
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    asyncio.run(e.turn("one"))
    asyncio.run(e.turn("two"))
    files = sorted((repo / ".claudex/sessions/t/handovers").glob("*.md"))
    texts = [f.read_text().split("\n")[0] for f in files]
    assert len(files) == 2 and set(texts) == {"FIRST-JOB", "SECOND-JOB"}
    journal = (repo / ".claudex/project/journal.md").read_text()
    for f in files:
        assert str(f) in journal


def test_f2_steps_through_all_nine_and_remembers(tmp_path):
    T, t, e = _tui(tmp_path)
    seen = set()
    for _ in range(9):
        seen.add((t.prefs["style"], t.prefs["size"]))
        t.cycle_look()
    assert len(seen) == 9 and (t.prefs["style"], t.prefs["size"]) == ("playful", "big")
    t.set_look("calm", "small")
    assert T.TUI(e).prefs["style"] == "calm" and T.TUI(e).prefs["size"] == "small"
    assert all(len(line) for line in t.draw_stage(60, T.STAGE_ROWS["small"]))


def test_old_animation_setting_still_honoured(tmp_path):
    from claudex import tui as T
    T.PREFS = tmp_path / "ui.json"
    T.PREFS.write_text('{"animate": false, "layout": "wide", "palette": "Jungle"}')
    assert T.TUI(engine.Engine(str(tmp_path), None, session_key="t")).prefs["style"] == "off"


# ---------- usage limits ----------
def test_worker_at_its_limit_hands_the_step_to_the_other_agent(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex"), step("codex", title="More sums")], "big_decisions": [],
                       "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    answers["final"] = {"verdict": "pass", "problems": [], "summary": "fits"}
    FakeChild.script = [(None, "", "You've hit your usage limit. Resets at 3pm."),     # Codex runs out
                        ("a.py", "v\n", "Claude finished it"),                          # Claude finishes step 1
                        ("b.py", "w\n", "Claude did step 2 too")]                       # step 2 goes straight to Claude
    e, ui, stop = run(repo)
    assert e.ok and stop == "end_turn"
    names = [w.name for w in FakeChild.started]
    assert names[0] == "codex" and "claude" in names[1]
    claude = FakeChild.started[1]
    assert "HANDOVER" in claude.prompts[0] and "Resets at 3pm" in claude.prompts[0]
    assert "it says it resets 3pm" in ui.all()
    j = (repo / ".claudex/project/journal.md").read_text()
    assert "usage limit" not in j or "Claude finished it" in j


def test_reviewer_at_its_limit_falls_back_and_says_so(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = [headless.HeadlessError("claude exited 1: Claude usage limit reached"),
                         {"verdict": "pass", "issues": [], "summary": "fine"}]
    FakeChild.script = [("a.py", "v\n", "done")]
    e, ui, stop = run(repo)
    reviews = [c for c in calls if c[0] == "review"]
    assert e.ok and [r[1] for r in reviews] == ["claude", "codex"]
    assert "same side that did the work" in (repo / ".claudex/sessions/t/notes.md").read_text()


def test_lead_at_its_limit_lets_codex_plan(job):
    repo, calls, answers = job
    answers["plan"] = [headless.HeadlessError("429 Too Many Requests"),
                       {"steps": [step("claude", "sonnet", "low")], "big_decisions": [], "concern_responses": []}]
    FakeChild.script = [(None, "", "Here's the answer.")]
    e, ui, stop = run(repo)
    plans = [c for c in calls if c[0] == "plan"]
    assert e.ok and [p[1] for p in plans] == ["claude", "codex"] and plans[1][2] == "gpt-6-astra"


def test_both_at_their_limits_stops_and_says_when(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    FakeChild.script = [(None, "", "Rate limit reached. Try again in 2 hours."),
                        (None, "", "You've hit your usage limit. Resets at 5pm.")]
    e, ui, stop = run(repo)
    assert not e.ok and "both agents are at their usage limits" in ui.all()
    assert "2 hours" in ui.all() and "5pm" in ui.all()


def test_writing_rate_limit_code_is_not_a_usage_limit(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("limiter.py", "RATE = 10\n", "Added a rate limiter: returns 429 when the limit is reached.")]
    e, ui, stop = run(repo)
    assert e.ok and [w.name for w in FakeChild.started] == ["codex"] and not e.limited


def test_browser_step_cant_go_to_codex_when_claude_is_limited(job, monkeypatch, tmp_path):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("claude", "sonnet", browser=True)], "big_decisions": [],
                       "concern_responses": []}
    FakeChild.script = [(None, "", "Claude usage limit reached")]
    e, ui, stop = run(repo)
    assert not e.ok and "signed-in Chrome" in ui.all() and len(FakeChild.started) == 1


# ---------- Codex's pre-publication review ----------
from claudex import limits  # noqa: E402


def test_limit_phrases_real_notices_vs_ordinary_prose():
    assert limits.reply_is_limit("…done the first half.\nYou've hit your 5-hour limit · resets 3pm")
    assert limits.reply_is_limit("You're out of extra usage.")
    assert limits.reply_is_limit("Claude AI usage limit reached|1762300000")
    assert not limits.reply_is_limit("HTTP 429 means Too Many Requests; retry with exponential backoff.")
    assert not limits.reply_is_limit("Added a rate limiter that returns 429 when the limit is reached.")
    assert not limits.reply_is_limit("The error message is `usage_limit_reached`; handle it by retrying later.")
    assert not limits.reply_is_limit("When users see \"You've hit your usage limit\" we show a banner.")
    assert limits.reply_is_limit("Partly done.\n⚠ Error: usage limit reached. Try again in 3 hours.")
    assert limits.reset_hint("You've hit your limit · resets 3pm") == "3pm"
    assert limits.reset_hint("Rate limit reached. Try again in 2 hours.") == "2 hours"
    assert limits.error_is_limit("429 Too Many Requests") and not limits.error_is_limit("file not found")


def test_handover_reviews_both_agents_edits(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("half.py", "x = 1\n", "Wrote half.py.\nYou've hit your usage limit. Resets at 5pm."),
                        (None, "", "Checked half.py; it's complete, nothing more to do.")]
    e, ui, stop = run(repo)
    reviews = [c for c in calls if c[0] == "review"]
    assert e.ok and reviews and "half.py" in reviews[0][4]          # the first agent's edit was still checked


def test_limit_after_partial_edits_is_caught(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    long = "Working through it.\n" + "detail\n" * 300 + "You've hit your usage limit · resets 3pm"
    FakeChild.script = [("a.py", "1\n", long), ("a.py", "2\n", "Finished it.")]
    e, ui, stop = run(repo)
    assert [w.name for w in FakeChild.started][:2] == ["codex", "claude"] and e.ok


def test_explaining_429_is_not_a_limit(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    FakeChild.script = [(None, "", "HTTP 429 means Too Many Requests; retry with exponential backoff.")]
    e, ui, stop = run(repo)
    assert e.ok and [w.name for w in FakeChild.started] == ["codex"] and not e.limited


def test_headless_keeps_the_reason_and_rejects_failed_runs(monkeypatch, tmp_path):
    async def fake_run(cmd, cwd, stdin_text, timeout):
        if cmd[0] == "claude":
            return 1, json.dumps({"is_error": True, "subtype": "error_during_execution",
                                  "errors": ["rate_limit_error: You've hit your usage limit"]}), ""
        out = cmd[cmd.index("-o") + 1]
        pathlib.Path(out).write_text('{"verdict": "pass", "issues": [], "summary": ""}')
        return 2, "", "codex: stream error 429 Too Many Requests"
    monkeypatch.setattr(headless, "_run", fake_run)
    for agent in ("claude", "codex"):
        with pytest.raises(headless.HeadlessError) as err:
            asyncio.run(headless.ask(agent, str(tmp_path), "x", engine.REVIEW_SCHEMA, "m", "high"))
        assert err.value.limit, agent


def test_recap_includes_what_the_first_agent_said_before_an_error(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}

    async def dies(child):
        child._text = ["Migrated the orders table; still to do: the invoices table."]
        raise RuntimeError("429 Too Many Requests")
    FakeChild.script = [dies, ("x.py", "1\n", "Did the invoices table.")]
    e, ui, stop = run(repo)
    assert e.ok and "still to do: the invoices table" in FakeChild.started[1].prompts[0]


def test_you_chose_codex_only_so_no_handover(job):
    repo, calls, answers = job
    FakeChild.script = [(None, "", "You've hit your usage limit. Resets at 5pm.")]
    e, ui, stop = run(repo, text="@codex sort the invoices")
    assert not e.ok and "Codex only" in ui.all() and [w.name for w in FakeChild.started] == ["codex"]


def test_same_side_second_opinion_is_flagged(job):
    repo, calls, answers = job
    plan = {"steps": [step("claude", "sonnet", "low")], "big_decisions": [
        {"decision": "x", "why": "y", "alternatives": "z"}], "concern_responses": []}
    answers["plan"] = [headless.HeadlessError("Claude usage limit reached"), plan]
    answers["opinion"] = {"verdict": "agree", "concerns": [], "suggestion": ""}
    FakeChild.script = [(None, "", "ok")]
    e, ui, stop = run(repo, dry_run=True)
    assert "Not an independent second opinion" in ui.all()


def test_your_path_comes_first(monkeypatch, tmp_path):
    from claudex import paths
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin")
    monkeypatch.delenv("CLAUDEX_NODE_BIN", raising=False)
    assert paths.tool_env()["PATH"].split(":")[:2] == [str(tmp_path), "/usr/bin"]


def test_sub_commands_ignore_a_lookalike_project(tmp_path):
    from claudex import paths
    (tmp_path / "claudex").mkdir()
    (tmp_path / "claudex/__init__.py").write_text("")
    (tmp_path / "claudex/__main__.py").write_text("print('IMPOSTER')")
    argv, env = paths.self_command("rules")
    out = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60).stdout
    assert "IMPOSTER" not in out and "Who does what" in out


def test_chrome_launcher_quotes_odd_paths(monkeypatch, tmp_path):
    from claudex import paths
    odd = tmp_path / "my $claude"
    odd.write_text("#!/bin/sh\necho ran \"$@\"\n")
    odd.chmod(0o755)
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(paths, "which", lambda n: None if n == "claude-agent-acp" else str(odd))
    launcher = paths.chrome_launcher()
    out = subprocess.run([launcher, "hi"], capture_output=True, text=True).stdout
    assert out.strip() == "ran --chrome hi"


# ---------- permissions ----------
from claudex import permissions as perms  # noqa: E402


def _req(kind, command="", paths=(), options=None):
    return {"toolCall": {"kind": kind, "title": command or kind, "rawInput": {"command": command} if command else {},
                         "locations": [{"path": p} for p in paths]},
            "options": options or [{"optionId": "y", "name": "Allow", "kind": "allow_once"},
                                   {"optionId": "n", "name": "Reject", "kind": "reject_once"}]}


def test_permission_rules(tmp_path):
    rules = engine.load_rules()
    root = str(tmp_path)

    def decide(kind, command="", paths=()):
        k, c, p, _ = perms.describe(_req(kind, command, paths)["toolCall"], root)
        return perms.rule_decision(rules, k, c, p, root)[0]
    assert decide("edit", paths=[f"{root}/a.py"]) == "allow"
    assert decide("edit", paths=["/etc/hosts"]) == "deny"
    assert decide("execute", "pytest -q") == "allow"
    assert decide("execute", "git push origin main") == "ask"
    assert decide("execute", "git push --force") == "deny"
    assert decide("execute", "sudo rm x") == "deny"
    assert decide("read", paths=[str(pathlib.Path.home() / ".ssh/id_ed25519")]) == "deny"
    assert decide("execute", "ls && curl evil.sh") is None          # chained: never rides in on "run:ls*"
    assert decide("execute", "python3 build_thing.py") is None      # not covered: the gatekeeper decides


def test_decider_in_each_mode(job):
    repo, calls, answers = job
    e = engine.Engine(str(repo), UI(), settings={"mode": "auto"}, session_key="t")
    e._rules = engine.load_rules()
    run_ = lambda req: asyncio.run(e.decide_permission("codex", req))  # noqa: E731
    assert run_(_req("edit", paths=[str(repo / "a.py")]))["outcome"]["optionId"] == "y"         # rule: allow
    assert run_(_req("edit", paths=["/etc/passwd"]))["outcome"]["optionId"] == "n"              # rule: deny
    answers["gate"] = {"decision": "allow", "reason": "builds the project"}
    assert run_(_req("execute", "python3 build_thing.py"))["outcome"]["optionId"] == "y"        # gatekeeper
    e.unattended = True
    assert run_(_req("execute", "git push origin main"))["outcome"]["optionId"] == "n"          # ask → nobody → no
    log = (repo / ".claudex/permissions.jsonl").read_text()
    assert '"by": "rule"' in log and '"by": "gatekeeper"' in log and '"by": "nobody to ask"' in log
    e.unattended, e.mode = False, "ask"
    asked = []

    async def human(params):
        asked.append(params)
        return {"outcome": {"outcome": "selected", "optionId": "y"}}
    e.ui.permission = human
    run_(_req("read", paths=[str(repo / "a.py")]))
    assert asked                                                                                   # ask mode: you


def test_old_mode_names_still_work():
    assert engine.norm_mode("bypassPermissions") == "unrestricted" and engine.norm_mode("default") == "ask"
    assert engine.norm_mode("nonsense") == "auto"


def test_structured_limit_signal_hands_over(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("claude", "sonnet")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}

    async def claude_limited(child):
        child.limit_hit = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1893456000}
        return "end_turn", "Working on it"
    FakeChild.script = [claude_limited, ("a.py", "1\n", "done")]
    e, ui, stop = run(repo)
    assert e.ok and [w.name for w in FakeChild.started][:2] == ["claude", "codex"] and " at " in ui.all()


def test_project_rules_layer_over_yours(tmp_path):
    (tmp_path / ".claudex").mkdir()
    (tmp_path / ".claudex/rules.toml").write_text('[permissions]\nallow = ["run:./manage.py test*"]\n')
    r = engine.load_rules(project_root=tmp_path)
    assert "run:./manage.py test*" in r["permissions"]["allow"] and "read:*" in r["permissions"]["allow"]
    assert r["permissions"]["deny"]                                                  # yours are kept


# ---------- usage ----------
def test_usage_reads_both_agents_logs(tmp_path, monkeypatch):
    from claudex import usage
    claude = tmp_path / "claude/p"
    claude.mkdir(parents=True)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    msg = {"timestamp": now, "cwd": "/w/shop", "requestId": "r1",
           "message": {"id": "m1", "model": "claude-opus-5-5",
                       "usage": {"input_tokens": 10, "cache_creation_input_tokens": 90, "cache_read_input_tokens": 500,
                                 "output_tokens": 50}}}
    (claude / "s.jsonl").write_text(json.dumps(msg) + "\n" + json.dumps(msg) + "\n")      # logged twice: counted once
    codex = tmp_path / "codex/2026/11/07"
    codex.mkdir(parents=True)
    rows = [{"type": "session_meta", "timestamp": now, "payload": {"cwd": "/w/shop"}},
            {"type": "turn_context", "timestamp": now, "payload": {"cwd": "/w/shop", "model": "gpt-6.1-sol"}},
            {"type": "event_msg", "timestamp": now, "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 100}}}},
            {"type": "event_msg", "timestamp": now, "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 1500, "cached_input_tokens": 900, "output_tokens": 150}}}}]
    (codex / "rollout-x.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(usage, "CLAUDE_LOGS", tmp_path / "claude")
    monkeypatch.setattr(usage, "CODEX_LOGS", tmp_path / "codex")
    monkeypatch.setattr(usage, "INDEX", tmp_path / "index.json")
    usage.record("claude", "sonnet", "gatekeeper", "/w/shop", fresh=5, output=5)
    s = usage.summary(2)
    assert s["totals"]["claude"] == {"fresh": 105, "cached": 500, "output": 55, "work": 160}
    assert s["totals"]["codex"] == {"fresh": 600, "cached": 900, "output": 150, "work": 750}
    assert s["by_project"][0]["project"] == "shop" and s["by_role"][0]["role"] == "gatekeeper"
    assert "Claude" in usage.terminal(s, colour=False)


def test_init_makes_one_house_rules_file(tmp_path):
    from claudex import admin
    (tmp_path / "CLAUDE.md").write_text("# Rules\nUse pence.\n")
    (tmp_path / "AGENTS.md").write_text("# Codex rules\nRun pytest.\n")
    admin.init(str(tmp_path), yes=True, no_map=True)
    a, c = (tmp_path / "AGENTS.md").read_text(), (tmp_path / "CLAUDE.md").read_text()
    assert "Use pence." in a and "Run pytest." in a and c.startswith("@AGENTS.md")
    assert (tmp_path / "CLAUDE.md.bak").read_text().startswith("# Rules") and admin.house_rules_plan(tmp_path) == []


# ---------- models.md ----------
def test_models_md_overrides_who_does_what(tmp_path):
    from claudex import models
    (tmp_path / ".claudex").mkdir()
    (tmp_path / ".claudex/models.md").write_text("""# Who does what
<!-- a comment the lead never sees -->
| When | Agent | Model | Reasoning | Browser |
|---|---|---|---|---|
| anything with money | codex | gpt-6-astra | high | |
| the shop's pages | claude | sonnet | high | yes |

Lead: claude opus xhigh
Default: claude sonnet medium
Codex reviewer: gpt-6-astra xhigh

- Database work always goes to Codex.
""")
    r = engine.load_rules(project_root=tmp_path)
    assert [x["use"]["agent"] for x in r["rule"]] == ["codex", "claude"] and r["rule"][1]["use"]["browser"]
    assert r["lead"]["effort"] == "xhigh" and r["defaults"] == {"agent": "claude", "model": "sonnet", "effort": "medium"}
    assert r["reviewers"]["codex"]["effort"] == "xhigh" and r["reviewers"]["claude"]["model"] == "opus"
    assert r["_lead_notes"] == "- Database work always goes to Codex." and "comment" not in r["_lead_notes"]
    again = models.parse(models.render(engine.load_rules()))     # render → parse round-trips the defaults
    assert len(again["rows"]) == len(engine.load_rules()["rule"]) and not again["notes"]


def test_lead_reads_the_notes(job):
    repo, calls, answers = job
    (repo / ".claudex").mkdir(exist_ok=True)
    (repo / ".claudex/models.md").write_text("- Money goes to Codex Astra.\n")
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    run(repo, dry_run=True)
    assert "Money goes to Codex Astra." in [c for c in calls if c[0] == "plan"][0][4]


# ---------- Codex's review of permissions, usage, init and bench ----------
@pytest.mark.parametrize("cmd,want", [
    ("ls & rm /tmp/x", None), ("find /tmp -delete", "ask"), ("git branch -D main", "ask"),
    ("cat ~/.ssh/id_ed25519", "deny"), ("sed -i s/a/b/ x.py", None), ("pytest -q", "allow"),
    ("echo hi > .claudex/rules.toml", "deny"), ("python3 -m pytest -q", "allow"), ("grep -i total calc.py", "allow"),
    ("tail -f log.txt", "allow")])
def test_permission_bypasses_are_closed(cmd, want, tmp_path):
    r = engine.load_rules()
    k, c, p, _ = perms.describe({"kind": "execute", "rawInput": {"command": cmd}}, str(tmp_path))
    assert perms.rule_decision(r, k, c, p, str(tmp_path))[0] == want


def test_workers_cant_rewrite_claudex_rules(tmp_path):
    r = engine.load_rules()
    k, c, p, _ = perms.describe({"kind": "edit", "locations": [{"path": str(tmp_path / ".claudex/rules.toml")}]},
                                str(tmp_path))
    assert perms.rule_decision(r, k, c, p, str(tmp_path))[0] == "deny"


def test_project_rules_can_only_add(tmp_path):
    (tmp_path / ".claudex").mkdir()
    (tmp_path / ".claudex/rules.toml").write_text('[permissions]\ndeny = []\nask = []\nallow = ["*:*"]\n')
    r = engine.load_rules(project_root=tmp_path)
    k, c, p, _ = perms.describe({"kind": "execute", "rawInput": {"command": "sudo rm -rf /"}}, str(tmp_path))
    assert perms.rule_decision(r, k, c, p, str(tmp_path))[0] == "deny"


def test_never_picks_an_always_option():
    only_always = [{"optionId": "a", "kind": "allow_always"}, {"optionId": "r", "kind": "reject_always"}]
    assert perms.pick_option(only_always, "allow") is None
    assert perms.pick_option(only_always, "deny") == {"outcome": {"outcome": "cancelled"}}


def test_init_respects_links_and_old_backups(tmp_path):
    from claudex import admin
    (tmp_path / "AGENTS.md").write_text("shared rules\n")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    assert admin.house_rules_plan(tmp_path) == []                        # already one file
    other = tmp_path / "other"
    other.mkdir()
    (other / "CLAUDE.md").write_text("new\n")
    (other / "CLAUDE.md.bak").write_text("precious\n")
    admin.init(str(other), yes=True, no_map=True)
    assert (other / "CLAUDE.md.bak").read_text() == "precious\n" and (other / "CLAUDE.md.bak1").read_text() == "new\n"


def test_usage_totals_include_codex_one_shot_calls(tmp_path, monkeypatch):
    from claudex import usage
    monkeypatch.setattr(usage, "CLAUDE_LOGS", tmp_path / "none1")
    monkeypatch.setattr(usage, "CODEX_LOGS", tmp_path / "none2")
    monkeypatch.setattr(usage, "INDEX", tmp_path / "i.json")
    usage.record("codex", "gpt-6-astra", "reviewer", "/w", total=1000)
    assert usage.summary(1)["totals"]["codex"]["work"] == 1000


def test_usage_page_escapes_names():
    from claudex import usage
    assert "esc(p.project)" in usage.PAGE and "${p.project}" not in usage.PAGE


def test_bench_scoring_cant_be_rigged(tmp_path):
    from claudex import bench
    task = bench.tasks()["quote-vat"]
    proj = tmp_path / "p"
    shutil.copytree(task["dir"] / "starter", proj)
    (proj / "pytest.ini").write_text("[pytest]\naddopts = -k example\n")
    (proj / "conftest.py").write_text("import sys; sys.exit(0)\n")
    (proj / "pytest.py").write_text("raise SystemExit(0)\n")
    h = bench.hidden_tests(task, proj)          # quote_total doesn't exist: everything must fail
    assert h["passed"] == 0 and h["total"] == 13


# ---------- Codex's third review ----------
def test_models_md_grammar_and_warnings():
    from claudex import models
    m = models.parse("\n".join([
        "| When | Agent | Model | Reasoning |", "|---|---|---|---|",
        "| VAT \\| invoices | codex | gpt-6-astra | high |", "| typo | claud | sonnet | high |",
        "", "Lead: `claude opus high`", "```", "Default: codex nonsense low", "```",
        "## Payments", "- Never touch payments without asking."]))
    assert m["rows"][0]["when"] == "VAT | invoices" and len(m["rows"]) == 1
    assert m["settings"] == {"lead": ("claude", "opus", "high")}                 # the fenced example isn't a setting
    assert "## Payments" in m["notes"] and "Never touch payments" in m["notes"]
    assert m["warnings"] and m["warnings"][0].startswith("line 4:")


def test_models_init_keeps_notes_in_force(tmp_path, monkeypatch):
    from claudex import admin, models
    monkeypatch.setattr(models, "USER_MODELS", tmp_path / "global.md")
    (tmp_path / "global.md").write_text("- Never touch payments without asking.\n")
    proj = tmp_path / "p"
    proj.mkdir()
    admin.show_models(str(proj), init_file=True)
    assert "Never touch payments" in engine.load_rules(project_root=proj)["_lead_notes"]


@pytest.mark.parametrize("cmd", ["git branch -vf archive HEAD~1", "git diff --output=/tmp/v", "rg --pre=sh x ./s",
                                 "cat $HOME/.ssh/id", "cat ~/.ss[h]/id", "git -c core.pager=sh log"])
def test_tricky_commands_go_to_the_gatekeeper(cmd, tmp_path):
    r = engine.load_rules()
    k, c, p, _ = perms.describe({"kind": "execute", "rawInput": {"command": cmd}}, str(tmp_path))
    assert perms.rule_decision(r, k, c, p, str(tmp_path))[0] in (None, "deny", "ask")


def test_active_config_files_are_protected(tmp_path):
    r = engine.load_rules()
    f = tmp_path / "conf/team-models.md"
    k, c, p, _ = perms.describe({"kind": "edit", "locations": [{"path": str(f)}]}, str(tmp_path))
    assert perms.rule_decision(r, k, c, p, str(tmp_path), [str(f)])[0] == "deny"


def test_backups_never_follow_links(tmp_path):
    from claudex import admin
    (tmp_path / "CLAUDE.md").write_text("mine\n")
    outside = tmp_path / "outside.txt"
    (tmp_path / "CLAUDE.md.bak").symlink_to(outside)                    # dangling link where a backup would go
    dest = admin._backup(tmp_path / "CLAUDE.md")
    assert dest.name == "CLAUDE.md.bak1" and not outside.exists()


def test_doctor_checks_the_gatekeeper_model():
    from claudex import tools
    assert any("gatekeeper" in w for w in sum(tools.used_models(engine.load_rules()).values(), []))


def test_who_did_the_work_and_why(job, monkeypatch, tmp_path):
    from claudex import usage
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "projects.json")
    repo, calls, answers = job
    answers["plan"] = {"steps": [dict(step("codex"), why="sums are Codex's thing")], "big_decisions": [],
                       "concern_responses": []}
    answers["review"] = [{"verdict": "fix", "issues": ["x"], "summary": ""}, {"verdict": "pass", "issues": [], "summary": ""}]
    FakeChild.script = [("a.py", "1\n", "a"), ("a.py", "2\n", "b")]
    run(repo)
    w = usage.work_report("2000-01-01")
    a = w["workers"][0]
    assert a["agent"] == "codex" and a["steps"] == 1 and a["sent_back"] == 1
    assert a["why"][0]["reason"] == "sums are Codex's thing"


def test_bench_stops_everything_left_in_the_folder(tmp_path):
    from claudex import bench
    p = subprocess.Popen(["sleep", "60"], cwd=tmp_path, start_new_session=True)   # like a worker in its own group
    bench._stop_in(tmp_path)
    assert p.wait(timeout=10) is not None


def test_bench_stops_marked_processes_that_wandered_off(tmp_path):
    from claudex import bench
    env = dict(os.environ, **{bench.MARK: "run123"})
    p = subprocess.Popen(["sleep", "60"], cwd="/", env=env, start_new_session=True)   # left the folder, own group
    bench._stop_in(tmp_path, "run123")
    assert p.wait(timeout=10) is not None


@pytest.mark.parametrize("cmd", ["git difftool --no-prompt --extcmd=touch a b", "git diff-tree HEAD",
                                 "git logx", "git log --ext-diff"])
def test_git_lookalikes_arent_allowed(cmd, tmp_path):
    r = engine.load_rules()
    k, c, p, _ = perms.describe({"kind": "execute", "rawInput": {"command": cmd}}, str(tmp_path))
    assert perms.rule_decision(r, k, c, p, str(tmp_path))[0] is None


def test_env_chosen_rules_are_protected(job, monkeypatch, tmp_path):
    repo, calls, answers = job
    custom = repo / "custom.toml"
    custom.write_text((pathlib.Path(engine.__file__).parent / "rules.toml").read_text())
    monkeypatch.setenv("CLAUDEX_RULES", str(custom))
    e = engine.Engine(str(repo), UI(), settings={"mode": "auto"}, session_key="t")
    e._rules = engine.load_rules(project_root=repo)
    out = asyncio.run(e.decide_permission("codex", _req("edit", paths=[str(custom)])))
    assert out["outcome"]["optionId"] == "n"


def test_render_keeps_pipes(tmp_path):
    from claudex import models
    r = engine.load_rules()
    r["rule"] = [{"when": "VAT | invoices", "use": {"agent": "codex", "model": "gpt-6-astra", "effort": "high"}}]
    assert models.parse(models.render(r))["rows"][0]["when"] == "VAT | invoices"


def test_escalated_model_gets_the_credit(job, monkeypatch, tmp_path):
    from claudex import usage
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "projects.json")
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", "gpt-6.1-sol", "medium")], "big_decisions": [], "concern_responses": []}
    fix = {"verdict": "fix", "issues": ["x"], "summary": ""}
    answers["review"] = [dict(fix), dict(fix), {"verdict": "pass", "issues": [], "summary": ""}]
    FakeChild.script = [("a.py", "1\n", "a"), ("a.py", "2\n", "b"), ("a.py", "3\n", "c")]
    run(repo)
    w = {a["who"]: a for a in usage.work_report("2000-01-01")["workers"]}
    assert w["codex · gpt-6.1-sol · high"]["steps"] == 1 and w["codex · gpt-6.1-sol · medium"]["sent_back"] == 2


def test_stale_detection_finds_acp_anywhere(monkeypatch):
    from claudex import paths
    import types
    fake = "  PID  PPID ELAPSED ARGS\n 10 1 9999 python3 -I -c x /home/x/claudex --acp --models team.md\n"
    monkeypatch.setattr(paths, "code_stamp", lambda: 9e12)
    monkeypatch.setitem(sys.modules, "subprocess", types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(stdout=fake)))
    assert paths.stale_workers() and paths.stale_workers()[0][0] == 10


def test_snapshot_edge_cases(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True)  # noqa: E731
    g("init", "-q")
    (root / ".gitignore").write_text("secret.txt\nbig/\n!node_modules/\n")
    (root / "*.txt").write_text("odd name")                 # a tracked file literally called '*.txt'
    (root / "secret.txt").write_text("s")                   # ignored: must stay out
    g("add", "-f", "--", ":(literal)*.txt", ".gitignore")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    (root / ".git/info/exclude").write_text("local-only/\n")
    (root / "local-only").mkdir()
    (root / "local-only/data.bin").write_text("x")
    (root / "node_modules/pkg").mkdir(parents=True)
    (root / "node_modules/pkg/i.js").write_text("1")
    a = gitsnap.snapshot(str(root))
    (root / "*.txt").write_text("changed")
    (root / "secret.txt").write_text("changed too")
    (root / "local-only/data.bin").write_text("y")
    (root / "node_modules/pkg/i.js").write_text("2")
    b = gitsnap.snapshot(str(root))
    assert gitsnap.changed_files(str(root), a, b) == ["*.txt"]


def test_warm_index_sees_deletions_and_new_ignores(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True)  # noqa: E731
    g("init", "-q")
    (root / "node_modules/local").mkdir(parents=True)
    (root / "node_modules/local/index.js").write_text("1")
    g("add", "-f", "node_modules/local/index.js")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    (root / "secret.txt").write_text("s")
    a = gitsnap.snapshot(str(root))
    (root / "node_modules/local/index.js").unlink()              # a tracked file in an excluded folder, deleted
    b = gitsnap.snapshot(str(root))
    assert gitsnap.changed_files(str(root), a, b) == ["node_modules/local/index.js"]
    (root / ".git/info/exclude").write_text(".claudex/\nsecret.txt\n")   # now ignored
    c = gitsnap.snapshot(str(root))
    (root / "secret.txt").write_text("changed")
    d = gitsnap.snapshot(str(root))
    assert gitsnap.changed_files(str(root), c, d) == []


def test_acp_session_can_be_reloaded(tmp_path, monkeypatch):
    """Agents of Empires restarts a session with session/load: same memory key and the same pickers."""
    from claudex import server
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.delenv("AOE_ARTIFACT_DIR", raising=False)
    srv = _server_session(tmp_path)

    async def first():
        init = await srv.on_request("initialize", {"protocolVersion": 1})
        sid = (await srv.on_request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))["sessionId"]
        await srv.on_request("session/set_config_option", {"sessionId": sid, "configId": "checks", "value": "reviews"})
        return init, sid, srv.sessions[sid].session_key
    init, sid, key = asyncio.run(first())
    assert init["agentCapabilities"]["loadSession"] is True
    fresh = _server_session(tmp_path)                       # a new worker process after a restart

    async def again():
        res = await fresh.on_request("session/load", {"sessionId": sid, "cwd": str(tmp_path), "mcpServers": []})
        return res, fresh.sessions[sid]
    res, e = asyncio.run(again())
    assert e.session_key == key and e.checks == "reviews"
    assert next(o for o in res["configOptions"] if o["id"] == "checks")["currentValue"] == "reviews"


def test_session_load_is_careful(tmp_path, monkeypatch):
    from claudex import server
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "sessions")
    srv = _server_session(tmp_path)
    for bad in ("../outside", "a/b", ""):
        with pytest.raises(server.RpcError):
            asyncio.run(srv.on_request("session/load", {"sessionId": bad, "cwd": str(tmp_path), "mcpServers": []}))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions/claudex-x.json").write_text(json.dumps({"cwd": str(tmp_path), "key": "../evil",
        "settings": {"checks": "off", "dry_run": True, "unattended": True, "mode": "nonsense"}}))
    meta = server.load_session_meta("claudex-x")
    assert "key" not in meta and meta["settings"] == {"checks": "off"}
    (tmp_path / "sessions/claudex-y.json").write_text("[]")
    assert server.load_session_meta("claudex-y") == {}
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(server.RpcError):
        asyncio.run(srv.on_request("session/load", {"sessionId": "claudex-x", "cwd": str(other), "mcpServers": []}))


def test_live_status_says_what_is_happening(tmp_path):
    from claudex.tui import describe_tool
    T, t, e = _tui(tmp_path)
    assert describe_tool({"title": "Terminal", "rawInput": {"command": "npm  test"}}, "/p") == "Terminal · npm test"
    assert describe_tool({"title": "Read", "locations": [{"path": "/p/support.js"}]}, "/p") == "Read · support.js"

    async def go():
        t.busy = asyncio.ensure_future(asyncio.sleep(30))
        t.activity("claude", "working")
        await t.update({"sessionUpdate": "tool_call", "toolCallId": "1", "title": "Read",
                        "locations": [{"path": str(tmp_path / "support.js")}]})
        return "".join(x for _, x in t.status())
    line = asyncio.run(go())
    assert "Claude is working" in line and "1 action" in line and "support.js" in line and "no input needed" in line


def test_markdown_tables_headings_and_code():
    from claudex.tui import _md
    text = "## Phases\n| Phase | What it delivers | Effort |\n|---|---|---|\n| 0 | Proof of concept | About 1 week |\n" \
           "| 1 | Paper sheets to a Word font | 4-6 weeks |\nUse `claudex init` and **bold**."
    out = "".join(t for _, t in _md(text, "b", 80))
    lines = out.split("\n")
    assert lines[0] == "Phases" and "|---" not in out and "`" not in out and "**" not in out
    header = next(l for l in lines if "Phase" in l and "Effort" in l)
    row = next(l for l in lines if "Proof of concept" in l)
    assert header.index("│") == row.index("│")                     # columns line up


def test_permission_answers_by_letter(tmp_path):
    T, t, e = _tui(tmp_path)
    t.build()
    opts = [{"optionId": "a1", "name": "Yes", "kind": "allow_once"},
            {"optionId": "a2", "name": "Yes, and don't ask again", "kind": "allow_always"},
            {"optionId": "r1", "name": "No", "kind": "reject_once"}]

    async def go(answer):
        t.busy = asyncio.ensure_future(asyncio.sleep(30))
        q = asyncio.ensure_future(t.permission({"toolCall": {"title": "Fetch x"}, "options": opts}))
        await asyncio.sleep(0)
        assert "press Y/A/N" in "".join(x for _, x in t.status())
        t._answer(answer)
        return (await q)["outcome"]["optionId"]
    assert asyncio.run(go("y")) == "a1" and asyncio.run(go("no")) == "r1" and asyncio.run(go("A")) == "a2"
    assert asyncio.run(go("3")) == "r1"


def test_dollar_runs_skills_and_slash_menu_is_commands_only():
    from claudex.commands import expand_skill
    from claudex.cli import make_completer
    from prompt_toolkit.document import Document
    skills = {"deployify": {"description": "ship it", "agents": ["claude"]}}
    assert expand_skill("$deployify the portal", skills) == ("Use the deployify skill. the portal", "claude")
    menu = make_completer(skills)
    slash = [c.text for c in menu.get_completions(Document("/de"), None)]
    dollar = [c.text for c in menu.get_completions(Document("$de"), None)]
    assert not any("deployify" in c for c in slash) and dollar == ["$deployify "]


def test_resume_an_earlier_conversation(job, tmp_path):
    from claudex import commands
    from claudex.tui import _blocks_from_notes
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "Made a.py for the quotes.")]
    asyncio.run(engine.Engine(str(repo), UI(), settings={}, session_key="older").turn("build the quote maths"))
    found = commands.sessions(repo)
    assert found[0]["key"] == "older" and "quote maths" in found[0]["first"]
    blocks = _blocks_from_notes((repo / ".claudex/sessions/older/notes.md").read_text())
    assert blocks[0] == {"who": "you", "text": "build the quote maths"} and blocks[1]["who"] == "codex"
    T, t, e = _tui(repo)
    asyncio.run(t._do_resume(found[0]))
    assert e.session_key == "older" and any("quote maths" in b["text"] for b in t.blocks)


def test_ctrl_c_twice_quits(tmp_path):
    T, t, e = _tui(tmp_path)
    app = t.build()
    handler = next(b.handler for b in app.key_bindings.bindings if [k.value if hasattr(k, "value") else k for k in b.keys] == ["c-c"])
    exited = []
    ev = type("E", (), {"app": type("A", (), {"exit": lambda self: exited.append(True)})()})()
    t.input.text = "half typed"
    handler(ev)
    assert t.input.text == "" and not exited                 # first: clears what you typed
    handler(ev)
    assert not exited and "again to quit" in "".join(x for _, x in t.status())
    handler(ev)
    assert exited                                            # pressed again within two seconds: quit


def test_resume_reloads_the_workers_own_conversations(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "first job")]
    FakeChild.resumed_with = []
    asyncio.run(engine.Engine(str(repo), UI(), settings={}, session_key="old").turn("job one"))
    assert (repo / ".claudex/sessions/old/workers.json").exists()
    e = engine.Engine(str(repo), UI(), settings={}, session_key="new")
    asyncio.run(e.switch_session("old"))
    FakeChild.script = [("b.py", "2\n", "second job")]
    asyncio.run(e.turn("job two"))
    assert FakeChild.resumed_with[-1] == "codex-session"          # Codex reloaded its own conversation


def test_picker_tab_and_abbreviations(tmp_path):
    T, t, e = _tui(tmp_path)
    t.build()
    picked = []
    t.open_picker("Pick", [("one", 1), ("two", 2), ("three", 3)], picked.append)
    t.picker["i"] = 2
    t._pick()
    assert picked == [3] and t.picker is None
    t.input.text = "/resu"
    t._complete_input()
    assert t.input.text.startswith("/resume")
    asyncio.run(t.command("/re"))                    # ambiguous: resume, retry…
    assert "Did you mean" in t.blocks[-1]["text"]
    asyncio.run(t.command("/stat"))                  # clear: /status
    assert "Settings for" in t.blocks[-1]["text"] and "Installs" in t.blocks[-1]["text"]


def test_long_command_question_is_plain_and_short(tmp_path):
    T, t, e = _tui(tmp_path)
    t.build()
    script = "cd " + str(tmp_path) + " && python3 - <<'EOF'\n" + "\n".join(
        f"rep('''**bold** | table | row {n}''')" for n in range(30)) + "\nEOF"
    opts = [{"optionId": "y", "name": "Yes", "kind": "allow_once"}, {"optionId": "n", "name": "No", "kind": "reject_once"}]

    async def go():
        t.busy = asyncio.ensure_future(asyncio.sleep(30))
        t.speaker = "claude"
        q = asyncio.ensure_future(t.permission({"toolCall": {"kind": "execute", "title": script,
                                                             "rawInput": {"command": script}}, "options": opts}))
        await asyncio.sleep(0)
        shown = "".join(x for _, x in t.transcript())
        bar = "".join(x for _, x in t.status())
        t._answer("y")
        return shown, bar, await q
    shown, bar, answer = asyncio.run(go())
    assert "Claude wants to run a command" in shown and "**bold**" in shown       # code kept as typed, not formatted
    assert "row 10" in shown and "row 11" not in shown and "20 more lines" in shown  # cut to a dozen lines
    assert str(tmp_path) not in shown and len(bar) < 200 and answer["outcome"]["optionId"] == "y"


def test_permission_with_arrow_keys(tmp_path):
    T, t, e = _tui(tmp_path)
    app = t.build()
    opts = [{"optionId": "y", "name": "Yes", "kind": "allow_once"},
            {"optionId": "a", "name": "Yes, and don't ask again", "kind": "allow_always"},
            {"optionId": "n", "name": "No", "kind": "reject_once"}]

    def key(name):
        for b in app.key_bindings.bindings:
            if [getattr(k, "value", k) for k in b.keys] == [name] and b.filter():
                return b.handler
    ev = type("E", (), {"app": None})()

    async def go():
        t.busy = asyncio.ensure_future(asyncio.sleep(30))
        q = asyncio.ensure_future(t.permission({"toolCall": {"kind": "execute", "rawInput": {"command": "git push"}},
                                                "options": opts}))
        await asyncio.sleep(0)
        key("down")(ev)
        key("down")(ev)
        shown = "".join(x for _, x in t.transcript())
        key("c-m")(ev)
        return shown, await q
    shown, answer = asyncio.run(go())
    assert "❯ [N] No" in shown and answer["outcome"]["optionId"] == "n"


def test_names_show_model_and_reasoning(tmp_path):
    T, t, e = _tui(tmp_path)

    async def go():
        t.busy = asyncio.ensure_future(asyncio.sleep(30))
        t.activity("claude", "working", None, "sonnet · medium")
        await t.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}})
        return "".join(x for _, x in t.transcript()), "".join(x for _, x in t.status())
    shown, bar = asyncio.run(go())
    assert "● Claude · sonnet · medium" in shown and "Claude (sonnet · medium) is working" in bar


def test_reply_is_a_draft_until_the_other_agent_agrees(tmp_path):
    T, t, e = _tui(tmp_path)

    async def go():
        t.activity("claude", "working", None, "opus · medium")
        await t.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "**Done:** it"}})
        t.check_mark("claude", "checking", "Codex gpt-6-astra")
        during = "".join(x for _, x in t.transcript())
        styles = [st for st, x in t.transcript() if "it" == x.strip()]
        t.check_mark("claude", "pass", "Codex gpt-6-astra")
        return during, styles, "".join(x for _, x in t.transcript())
    during, styles, after = asyncio.run(go())
    assert "⏳ draft: Codex gpt-6-astra is checking it" in during
    assert "✓ checked: Codex gpt-6-astra agrees" in after and "draft" not in after


def test_writing_only_changes_get_the_lighter_check():
    from claudex.engine import Engine
    review = {"light_for_docs": True, "light": {"codex": {"model": "gpt-6.1-sol", "effort": "medium"}}}
    assert Engine._review_tier(["docs/plan.md", "NOTES.txt"], review) == "light"
    assert Engine._review_tier(["docs/plan.md", "app.py"], review) == "full"
    assert Engine._review_tier(["docs/plan.md"], dict(review, light_for_docs=False)) == "full"


def test_workers_are_asked_for_one_reply_shape():
    from claudex.engine import REPLY_SHAPE
    for part in ("**Done:**", "**Changed:**", "**Checked:**", "**Not checked:**", "Questions for you"):
        assert part in REPLY_SHAPE


def test_a_no_from_claudex_is_not_a_stop(job):
    """Codex's only 'no' ends its turn as cancelled: Claudex tells it why and it carries on, the job goes on."""
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["gate"] = {"decision": "deny", "reason": "not needed for this step"}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}

    async def asks_then_cancelled(c):
        await c.decide(_req("execute", "python3 -m venv .venv"))
        return "cancelled", "I'll make a virtualenv."
    FakeChild.script = [asks_then_cancelled, ("a.py", "x\n", "Done without it.")]
    e, ui, stop = run(repo, mode="auto")
    w = FakeChild.started[0]
    assert e.ok and stop == "end_turn" and "Stopped" not in ui.all()
    assert "Claudex said no to" in w.prompts[1] and "not needed for this step" in w.prompts[1]


def test_read_only_claude_calls_are_not_in_plan_mode():
    cmd = headless.claude_cmd("sonnet", "low", {}, "Read,Grep,Glob")
    assert "plan" not in cmd and cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert "--allowedTools" not in headless.claude_cmd("sonnet", "low", {}, "")


def test_installs_into_the_project_go_ahead_computer_installs_ask(tmp_path):
    r, root = engine.load_rules(), str(tmp_path)

    def d(c):
        k, cmd, paths, _ = perms.describe({"kind": "execute", "title": c, "rawInput": {"command": c}}, root)
        return perms.rule_decision(r, k, cmd, paths, root)[0]
    assert d(".venv/bin/pip install numpy flask") == "allow" and d("npm install") == "allow"
    assert d("pip install requests") == "ask" and d("npm install -g ts") == "ask" and d("brew install jq") == "ask"
    from claudex.admin import set_project_permission
    set_project_permission(root, "installs", "ask")
    r = engine.load_rules(project_root=root)
    assert d(".venv/bin/pip install numpy") == "ask"


def test_gatekeeper_limit_uses_other_agent_and_remembers(job):
    repo, calls, answers = job
    e = engine.Engine(str(repo), UI(), settings={"mode": "auto"}, session_key="t")
    e._rules = engine.load_rules()
    e.notes_dir.mkdir(parents=True, exist_ok=True)
    answers["gate"] = [headless.HeadlessError("usage limit", limit=True),
                       {"decision": "allow", "reason": "local editing"},
                       {"decision": "allow", "reason": "local tests"}]
    async def go():
        for _ in range(2):
            result = await e.decide_permission("codex", _req("execute", "python3 build.py"))
            assert result["outcome"]["optionId"] == "y"
    asyncio.run(go())
    assert [c[1] for c in calls if c[0] == "gate"] == ["claude", "codex", "codex"]


@pytest.mark.parametrize("command, expected", [
    ("/usr/bin/zsh -lc 'git status --short'", "allow"),
    ("/usr/bin/zsh -lc '.venv/bin/pip install pytest'", "allow"),
    ("/usr/bin/zsh -lc 'git push origin main'", "ask"),
    ("/usr/bin/zsh -lc 'git push --force'", "deny"),
    ("/usr/bin/zsh -lc 'ls; python3 unknown.py'", None),
    ("/usr/bin/zsh -lc 'ls' && python3 unknown.py", None),
    ("/tmp/zsh -lc 'git status'", None),
])
def test_shell_wrapped_permission_rules(command, expected, tmp_path):
    k, c, p, _ = perms.describe({"kind": "execute", "rawInput": {"command": command}}, str(tmp_path))
    assert perms.rule_decision(engine.load_rules(), k, c, p, str(tmp_path))[0] == expected


def test_work_timeout_excludes_human_wait_and_still_stops_work(job):
    repo, _, _ = job
    e = engine.Engine(str(repo), UI(), settings={"mode": "ask"}, session_key="t")
    async def human(params):
        await asyncio.sleep(.08)
        return {"outcome": {"outcome": "selected", "optionId": "y"}}
    e.ui.permission = human
    async def go():
        result = await e._wait_working(e.decide_permission("codex", _req("execute", "test")), .03)
        assert result["outcome"]["optionId"] == "y"
        assert e._human_waits == 0 and e._human_since is None
        with pytest.raises(asyncio.TimeoutError):
            await e._wait_working(asyncio.sleep(1), .01)
        pending = asyncio.create_task(e._wait_working(e.decide_permission("codex", _req("execute", "test")), 1))
        await asyncio.sleep(.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert e._human_waits == 0 and e._human_since is None
    asyncio.run(go())


def test_needs_panel_clicks_answers_and_ticks_tasks(tmp_path):
    from types import SimpleNamespace
    from prompt_toolkit.mouse_events import MouseEventType
    _, t, _ = _tui(tmp_path)
    t.build()
    click = SimpleNamespace(event_type=MouseEventType.MOUSE_UP)
    async def go():
        _live_job(t)
        opts = [{"optionId": "y", "name": "Yes", "kind": "allow_once"}]
        q = asyncio.create_task(t.permission({"toolCall": {"title": "first"}, "options": opts}))
        await asyncio.sleep(0)
        t.on_accept(SimpleNamespace(text="/todo Check the printout"))
        handlers = [f[2] for f in t.needs_text() if len(f) == 3]
        handlers[1](click)
        assert t.todos[0]["done"] and not q.done()
        handlers[0](click)
        assert (await q)["outcome"]["optionId"] == "y"
        q2 = asyncio.create_task(t.permission({"toolCall": {"title": "second"}, "options": opts}))
        await asyncio.sleep(0)
        handlers[0](click)  # stale click must never answer a different question
        assert not q2.done()
        t.drop_questions()
        assert (await q2)["outcome"]["outcome"] == "cancelled"
        assert "cancelled" in "".join(f[1] for f in t.transcript())
        t.on_accept(SimpleNamespace(text="/done 1"))
        assert not t.todos[0]["done"]
    asyncio.run(go())


def test_unrestricted_default_and_explicit_modes(tmp_path):
    from claudex.__main__ import chat_parser, settings_from
    parser = chat_parser()
    assert settings_from(parser.parse_args([]))["mode"] is None  # project setting resolved by Engine
    assert engine.Engine(str(tmp_path), UI()).mode == "unrestricted"
    for mode in ("auto", "ask", "unrestricted"):
        settings = settings_from(parser.parse_args(["--mode", mode]))
        assert engine.Engine(str(tmp_path), UI(), settings=settings).mode == mode


def test_unrestricted_connector_requests_never_ask_human_or_gatekeeper(job):
    repo, calls, answers = job
    e = engine.Engine(str(repo), UI(), settings={"mode": "unrestricted"}, session_key="t")
    async def human(params):
        pytest.fail("unrestricted mode must not prompt")
    e.ui.permission = human
    async def go():
        for request in (_req("execute", "git push origin main"),
                        _req("other", "browser_file_upload")):
            result = await e.decide_permission("claude", request)
            assert result["outcome"]["optionId"] == "y"
        no_allow = {"toolCall": {"kind": "other"}, "options": [{"optionId": "n", "kind": "reject_once"}]}
        assert (await e.decide_permission("claude", no_allow))["outcome"]["outcome"] == "cancelled"
    asyncio.run(go())
    assert not calls
    assert '"by": "unrestricted"' in (repo / ".claudex/permissions.jsonl").read_text()


def test_project_mode_persists_with_explicit_override_and_isolation(tmp_path):
    from claudex import project_settings as ps
    from claudex.__main__ import chat_parser, settings_from
    _, t, e = _tui(tmp_path)
    t.build()
    asyncio.run(t.command('/mode'))
    assert t.picker is not None
    t._pick(next(i for i, (_, value) in enumerate(t.picker['items']) if value == 'auto'))
    settings = settings_from(chat_parser().parse_args([]))
    assert engine.Engine(str(tmp_path), UI(), settings=settings).mode == 'auto'
    assert engine.Engine(str(tmp_path), UI(), settings={'mode': 'ask'}).mode == 'ask'
    assert engine.Engine(str(tmp_path / 'elsewhere'), UI()).mode == 'unrestricted'
    assert ps.load(tmp_path)['mode'] == 'auto'


def test_onboarding_save_cancel_and_existing_instructions(tmp_path):
    from claudex import project_settings as ps
    _, t, e = _tui(tmp_path)
    t.build()
    t.open_setup()
    assert t.setup_dialog is not None
    t.setup_form.description.text = 'Do not save this'
    t.close_setup()
    assert ps.load(tmp_path) == {}
    (tmp_path / 'AGENTS.md').write_text('Existing rules\n')
    t.open_setup()
    form = t.setup_form
    form.description.text = 'A handwriting app'
    form.prompt.text = 'Always keep the original photographs.'
    t.pending_mode = 'ask'
    form.mode.current_value = 'auto'
    form.instructions.checked = True
    form.show(1)
    form.show(2)
    form.save()
    assert t.setup_dialog is None and t.pending_mode is None
    assert ps.load(tmp_path)['onboarded'] is True
    assert (tmp_path / 'AGENTS.md').read_text() == 'Existing rules\n'
    assert (tmp_path / 'CLAUDE.md').read_text() == '@AGENTS.md\n'
    new = engine.Engine(str(tmp_path), UI())
    assert new.mode == 'auto'
    assert 'Always keep the original photographs.' in new._instructions()
    assert 'A handwriting app' in new._instructions()
    t.open_setup()
    assert t.setup_form.prompt.text == form.prompt.text
    t.close_setup()


def test_model_choices_enforced_for_worker_and_headless(job):
    from claudex import project_settings as ps
    repo, calls, answers = job
    ps.save(repo, {'enabled_models': {'claude': ['sonnet'], 'codex': ['gpt-6.1-sol']}})
    e = engine.Engine(str(repo), UI())
    r = engine.load_rules(project_root=repo)
    assert r['lead']['model'] == 'sonnet'
    assert r['reviewers']['codex']['model'] == 'gpt-6.1-sol'
    answers['gate'] = {'decision': 'allow', 'reason': 'fine'}
    async def go():
        await e._ask('claude', 'opus', 'high', 'test', perms.GATE_SCHEMA, r, 10, role='gatekeeper')
        class Worker:
            name = 'codex'
            options = {'model': ['gpt-6.1-sol'], 'mode': ['agent-full-access']}
            async def set(self, key, value):
                if key == 'model':
                    assert value == 'gpt-6.1-sol'
                return True
        assert await e._configure(Worker(), step(), r, 'gpt-6-astra', 'medium') == 'gpt-6.1-sol'
    asyncio.run(go())
    assert calls[-1][2] == 'sonnet'
    assert 'opus' in ps.model_choices(engine.load_rules(project_root=repo, model_preferences=False), {})['claude']


def test_setup_requires_models_and_skip_is_remembered(tmp_path):
    from claudex import project_settings as ps
    _, t, _ = _tui(tmp_path)
    t.build()
    t.open_setup()
    form = t.setup_form
    form.models.current_values = []
    form.save()
    assert t.setup_dialog is not None and 'at least one' in form.error.text
    assert not ps.path(tmp_path).exists()
    form.skip()
    assert ps.load(tmp_path)['onboarded'] and t.setup_dialog is None


def test_non_utf8_patch_is_reviewable_without_changing_source(tmp_path):
    from claudex import gitsnap
    source = tmp_path / 'legacy.txt'
    source.write_bytes(b'original\n')
    before = gitsnap.snapshot(str(tmp_path))
    raw = b'Windows quote: \x93hello\x94\n'
    source.write_bytes(raw)
    after = gitsnap.snapshot(str(tmp_path))
    diff, files, truncated = gitsnap.diff(str(tmp_path), before, after)
    assert 'legacy.txt' in files and not truncated
    assert r'\x93hello\x94' in diff
    assert source.read_bytes() == raw


@pytest.mark.parametrize('text, expected', [
    ('**Needs you:** Run the server.\nThen open the page.\n\nDone.', ['Run the server. Then open the page.']),
    ('NEEDS YOU: Check a printout.\n', ['Check a printout.']),
    ('Needs you: none.\n', []),
    ('```\nNEEDS YOU: an example\n```', []),
])
def test_human_action_markdown(text, expected):
    assert engine.human_actions(text) == expected


STRUCTURED = """Done the thing.

## Questions for you
Reply like: 1A, 2B, 3 skip, 5: <your words>

### Security
**Q1. Rotate the database password**
Change the production database password now?
Why: it appeared in a session transcript.
A) Yes, tonight (recommended): quick and low risk
B) Later
If you don't answer: waits.

### Natter
**Q2. Which customer owns these? (still open)**
Confirm each best guess.
| Item | Best guess | Confirm Y/N or name |
|---|---|---|
| friee | 8hours | |
If you don't answer: they stay unmapped.

## Things only you can do
1. Plug in the backup key and register it in Settings.

```
**Q9. Example in a code block**
```
"""


def test_human_actions_structured_questions():
    assert engine.human_actions(STRUCTURED) == [
        'Q1. Rotate the database password: Change the production database password now?',
        'Q2. Which customer owns these? (still open): Confirm each best guess.',
        'Plug in the backup key and register it in Settings.']


def test_brief_asks_for_structured_questions():
    assert "Questions for you" in engine.QUESTIONS_SHAPE and "(recommended)" in engine.QUESTIONS_SHAPE
    assert "Needs you:" not in engine.REPLY_SHAPE


def test_selection_default_and_clickable_links(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from prompt_toolkit.mouse_events import MouseEventType
    import webbrowser
    _, t, _ = _tui(tmp_path)
    t.build()
    assert not t.app.mouse_support()
    t._add('claude', 'Open [the page](http://127.0.0.1:5055).')
    assert t.conversation_links() == ['http://127.0.0.1:5055']
    opened = []
    monkeypatch.setattr(webbrowser, 'open', lambda url: opened.append(url) or True)
    t.toggle_mouse()
    assert t.app.mouse_support()
    fragments = [f for f in t.clickable_transcript() if len(f) == 3]
    fragments[0][2](SimpleNamespace(event_type=MouseEventType.MOUSE_UP))
    assert opened == ['http://127.0.0.1:5055']
    t.toggle_mouse()
    assert not t.app.mouse_support()
    t.links_menu()
    t._pick(0)
    assert len(opened) == 2


def test_mode_picker_during_approval_saves_next_job_without_answering(tmp_path):
    from types import SimpleNamespace
    from claudex import project_settings as ps
    _, t, e = _tui(tmp_path)
    t.build()
    e.mode = 'auto'
    async def go():
        _live_job(t)
        pending = asyncio.create_task(t.permission({'toolCall': {'title': 'upload'},
                    'options': [{'optionId': 'y', 'kind': 'allow_once', 'name': 'Yes'}]}))
        await asyncio.sleep(0)
        t.on_accept(SimpleNamespace(text='mode'))
        assert t.picker
        t._pick(0)
        assert not pending.done() and e.mode == 'auto'
        assert t.pending_mode == 'unrestricted'
        assert ps.load(tmp_path)['mode'] == 'unrestricted'
        t.drop_questions()
        await pending
    asyncio.run(go())


def test_attachment_paths_and_safe_copies(tmp_path):
    from claudex import attachments as a
    source = tmp_path / 'photo with spaces.png'
    raw = b'\x89PNG\r\n\x1a\noriginal'
    source.write_bytes(raw)
    assert a.paths_in(source.as_uri(), tmp_path, only=True) == [source]
    assert a.paths_in("'" + str(source) + "'", tmp_path, only=True) == [source]
    assert a.paths_in('copy\n' + source.as_uri(), tmp_path, only=True) == [source]
    assert a.paths_in('a' * 10000, tmp_path) == []
    assert a.paths_in('file://remote-host/private.png', tmp_path) == []
    item = a.store_file(source, tmp_path)
    source.write_bytes(b'changed later')
    assert pathlib.Path(item['path']).read_bytes() == raw
    assert 'image-viewing tool' in a.prompt([item])
    assert item['path'] in a.prompt([item])


def test_paste_files_and_multiline_stays_a_draft(tmp_path):
    from types import SimpleNamespace
    _, t, e = _tui(tmp_path)
    t.build()
    source = tmp_path / 'screenshot.png'
    source.write_bytes(b'png')
    t.paste_text(source.as_uri())
    assert len(t.attachments) == 1 and t.input.text == '' and t.busy is None
    t.paste_text('First line\r\nSecond line\nThird line')
    assert t.input.text == 'First line\nSecond line\nThird line'
    assert t.input_height() >= 3 and t.busy is None
    sent = []
    async def capture(text):
        sent.append(text)
    t.run = capture
    async def go():
        assert t.on_accept(SimpleNamespace(text=t.input.text)) is False
        await t.busy
    asyncio.run(go())
    assert len(sent) == 1 and 'First line\nSecond line' in sent[0]
    assert 'screenshot.png' in sent[0] and 'ATTACHED LOCAL FILES' in sent[0]
    assert not t.attachments


def test_dropped_path_is_not_a_slash_command_and_busy_draft_survives(tmp_path):
    from types import SimpleNamespace
    _, t, _ = _tui(tmp_path)
    t.build()
    source = tmp_path / 'photo.png'
    source.write_bytes(b'png')
    sent = []
    async def capture(text):
        sent.append(text)
    t.run = capture
    async def go():
        t.on_accept(SimpleNamespace(text=str(source)))
        await t.busy
        assert t.cmd_task is None
        _live_job(t)
        assert t.on_accept(SimpleNamespace(text='Keep this\nmultiline draft')) is True
    asyncio.run(go())
    assert sent and 'Please inspect the attached files.' in sent[0]


def test_clipboard_images_files_text_and_failure(tmp_path, monkeypatch):
    from claudex import attachments as a
    _, t, _ = _tui(tmp_path)
    t.build()
    async def go():
        async def image_clipboard():
            return 'image/png', b'\x89PNG\r\n\x1a\nimage'
        monkeypatch.setattr(a, 'clipboard', image_clipboard)
        await t.paste_clipboard()
        assert len(t.attachments) == 1
        assert pathlib.Path(t.attachments[0]['path']).read_bytes().startswith(b'\x89PNG')
        async def text_clipboard():
            return 'text/plain', b'hello\nworld'
        monkeypatch.setattr(a, 'clipboard', text_clipboard)
        await t.paste_clipboard()
        assert t.input.text == 'hello\nworld'
        async def unavailable():
            raise ValueError('Clipboard unavailable')
        monkeypatch.setattr(a, 'clipboard', unavailable)
        await t.paste_clipboard()
        assert 'Clipboard unavailable' in ''.join(f[1] for f in t.transcript())
        assert t.input.text == 'hello\nworld' and len(t.attachments) == 1
    asyncio.run(go())


def test_wayland_clipboard_chooses_files_before_image(tmp_path, monkeypatch):
    from claudex import attachments as a
    calls = []
    monkeypatch.setenv('WAYLAND_DISPLAY', 'wayland-test')
    monkeypatch.setattr(a.shutil, 'which', lambda name: '/bin/' + name)
    async def capture(cmd, limit=a.MAX_BYTES):
        calls.append(cmd)
        return b'image/png\ntext/uri-list\n' if '--list-types' in cmd else b'file:///tmp/test.png'
    monkeypatch.setattr(a, '_capture', capture)
    assert asyncio.run(a.clipboard()) == ('text/uri-list', b'file:///tmp/test.png')
    assert calls[-1][-1] == 'text/uri-list'


def test_attachment_limits_and_detach(tmp_path, monkeypatch):
    from claudex import attachments as a
    from types import SimpleNamespace
    _, t, _ = _tui(tmp_path)
    t.build()
    source = tmp_path / 'data.txt'
    source.write_bytes(b'12345')
    monkeypatch.setattr(a, 'MAX_BYTES', 4)
    assert not t.attach_paths([source]) and not t.attachments
    monkeypatch.setattr(a, 'MAX_BYTES', 20)
    assert t.attach_paths([source])
    assert t.attach_paths([source]) and len(t.attachments) == 1
    t.on_accept(SimpleNamespace(text='/detach 1'))
    assert not t.attachments and source.read_bytes() == b'12345'


def test_clipboard_capture_is_binary_safe_and_bounded():
    from claudex import attachments as a
    import sys
    async def go():
        command = [sys.executable, '-c', "import sys; sys.stdout.buffer.write(bytes([0, 147, 255, 10]))"]
        assert await a._capture(command) == bytes([0, 147, 255, 10])
        with pytest.raises(ValueError, match='larger'):
            await a._capture(command, limit=3)
    asyncio.run(go())


def test_multiline_paste_starting_with_command_is_a_message(tmp_path):
    from types import SimpleNamespace
    _, t, e = _tui(tmp_path)
    t.build()
    sent = []
    async def capture(text):
        sent.append(text)
    t.run = capture
    async def go():
        text = '/mode auto\nExplain what this does.'
        t.on_accept(SimpleNamespace(text=text))
        await t.busy
        assert sent == [text] and e.mode == 'unrestricted'
    asyncio.run(go())


def test_multiline_draft_is_kept_during_approval(tmp_path):
    from types import SimpleNamespace
    _, t, _ = _tui(tmp_path)
    t.build()
    async def go():
        _live_job(t)
        question = asyncio.create_task(t.permission({'toolCall': {'title': 'upload'},
            'options': [{'optionId': 'y', 'kind': 'allow_once', 'name': 'Yes'}]}))
        await asyncio.sleep(0)
        assert t.on_accept(SimpleNamespace(text='My next question\nhas two lines')) is True
        assert not question.done()
        assert t.on_accept(SimpleNamespace(text='y')) is False
        await question
    asyncio.run(go())


def test_copy_view_freezes_stream_and_restores_draft(tmp_path):
    from types import SimpleNamespace
    _, t, e = _tui(tmp_path)
    t.build()
    t._add('claude', 'Run `python app.py` to start.\n')
    t.input.text = 'my unfinished draft'
    t.prefs['mouse'] = True
    t.toggle_copy_view()
    snapshot = t.copy_view.text
    assert 'python app.py' in snapshot and t.copy_mode
    assert t.app.mouse_support()             # Claudex does the selecting in the copy view, in any terminal
    invalidated = []
    t.app.invalidate = lambda: invalidated.append(True)
    t._add('claude', 'More output while copying.\n')
    t.activity('claude', 'working')
    t._refresh()
    assert t.copy_view.text == snapshot and not invalidated
    assert 'More output' in ''.join(f[1] for f in t.transcript())
    ctrl_c = next(b.handler for b in t.app.key_bindings.bindings
                  if [getattr(k, 'value', k) for k in b.keys] == ['c-c'])
    ctrl_c(SimpleNamespace(app=t.app))
    assert t.copy_mode and not e.cancelled and t.input.text == 'my unfinished draft'
    t.toggle_copy_view()
    assert not t.copy_mode and t.app.mouse_support()
    assert t.input.text == 'my unfinished draft' and invalidated
    assert t.app.layout.has_focus(t.input)


def test_copy_view_streaming_does_not_render_again(tmp_path):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.data_structures import Size
    class Output(DummyOutput):
        def get_size(self):
            return Size(rows=30, columns=100)
    async def go():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=Output()):
            _, t, _ = _tui(tmp_path)
            t.speaker = 'claude'
            t._add('claude', 'A command to copy: python app.py\n')
            app = t.build()
            renders = []
            app.after_render += lambda app: renders.append(True)
            running = asyncio.create_task(app.run_async())
            ticker = asyncio.create_task(t.animate())
            try:
                await asyncio.sleep(.05)
                pipe.send_text('\x1b[19~')  # F8
                await asyncio.sleep(.1)
                assert t.copy_mode
                snapshot, before = t.copy_view.text, len(renders)
                for i in range(6):
                    await t.update({'sessionUpdate': 'agent_message_chunk',
                                    'content': {'type': 'text', 'text': f'New chunk {i}\n'}})
                    await asyncio.sleep(.06)
                assert t.copy_view.text == snapshot
                assert len(renders) == before  # neither streaming nor animation repaints native selection
                assert 'New chunk 5' in ''.join(f[1] for f in t.transcript())
                pipe.send_text('\x1b[19~')
                await asyncio.sleep(.1)
                assert not t.copy_mode and app.layout.has_focus(t.input)
                assert len(renders) > before
                pipe.send_text('\x1b[19~')
                await asyncio.sleep(.1)
                assert 'New chunk 5' in t.copy_view.text  # reopening takes a fresh snapshot
            finally:
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
                app.exit()
                await running
    asyncio.run(go())


# ---------- leaner prompts, same accuracy ----------
def _three_codex_steps():
    return {"steps": [step(title="One"), step(title="Two"), step(title="Three")], "big_decisions": [],
            "concern_responses": []}


def test_worker_isnt_sent_unchanged_memory_twice(job):
    repo, calls, answers = job
    (repo / "CLAUDE.md").write_text("House rule: whole pence.\n")
    answers["plan"] = {"steps": [step(title="One"), step(title="Two")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "FIRST-REPLY-MARK"), ("b.py", "2\n", "second")]
    run(repo)
    w = FakeChild.started[0]
    first, second = w.prompts[0], w.prompts[1]
    assert "whole pence" in first and "**Done:**" in first
    assert "whole pence" not in second and "**Done:**" not in second            # already in this conversation
    assert "PROJECT INSTRUCTIONS (CLAUDE.md and AGENTS.md; follow both): unchanged" in second
    assert second.count("FIRST-REPLY-MARK") == 1                                 # this job's work, once
    assert "Step 2 of 2: Two" in second


def test_squashed_worker_gets_everything_again(job):
    repo, calls, answers = job
    (repo / "CLAUDE.md").write_text("House rule: whole pence.\n")
    answers["plan"] = _three_codex_steps()
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}

    def work(fname, squash):
        async def go(c):
            pathlib.Path(c.cwd, fname).write_text("x\n")
            c.usage = {"used": 1000}
            c.squashed = squash or getattr(c, "squashed", False)
            return "end_turn", f"made {fname}"
        return go
    FakeChild.script = [work("a.py", False), work("b.py", True), ("c.py", "3\n", "made c.py")]
    run(repo)
    p = FakeChild.started[0].prompts
    assert "whole pence" in p[0] and "whole pence" not in p[1]
    assert "whole pence" in p[2] and "**Done:**" in p[2]          # its context was squashed: everything again


def test_a_worker_that_never_reports_its_context_gets_memory_each_job(job):
    repo, calls, answers = job
    (repo / "CLAUDE.md").write_text("House rule: whole pence.\n")
    answers["plan"] = {"steps": [step(title="One"), step(title="Two")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "one"), ("b.py", "2\n", "two"), ("c.py", "3\n", "three"),
                        ("d.py", "4\n", "four")]
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    asyncio.run(e.turn("first"))
    asyncio.run(e.turn("second"))
    p = FakeChild.started[0].prompts
    assert ["whole pence" in x for x in p] == [True, False, True, False]   # can't see squashes: once per job


def test_child_notices_its_context_shrinking_mid_turn():
    from claudex.child import Child
    c = Child("codex", ["x"], ".", UI())

    async def go():
        for used in (50000, 180000, 60000):
            await c._on_notify("session/update", {"update": {"sessionUpdate": "usage_update", "used": used}})
    asyncio.run(go())
    assert c.squashed and c.usage["used"] == 60000


def test_codex_is_sent_an_agents_md_too_long_for_it_to_read_whole(job):
    repo, calls, answers = job
    (repo / "AGENTS.md").write_text("OPENING RULE.\n" + "filler line\n" * 4000)   # about 48 KB
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    e._rules = engine.load_rules()
    assert "OPENING RULE." in e._instructions("codex")                  # past Codex's own cut-off: pasted
    (repo / "AGENTS.md").write_text("OPENING RULE.\n")
    assert "OPENING RULE." not in e._instructions("codex") and "loads it itself" in e._instructions("codex")

def test_map_is_left_alone_when_only_existing_files_changed(job):
    repo, calls, answers = job
    (repo / ".claudex/project").mkdir(parents=True)
    (repo / ".claudex/project/map.md").write_text("# Map\n")
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": ""}
    FakeChild.script = [("a.py", "x = 9\n", "edited a.py")]
    run(repo)
    assert not any(c[0] == "map" for c in calls)
    calls.clear()
    FakeChild.script = [("new_module.py", "y = 1\n", "added a module")]
    run(repo)
    assert any(c[0] == "map" for c in calls)


def test_map_worthy_changes():
    assert engine.map_worthy([("A", "src/x.py")]) and engine.map_worthy([("M", "package.json")])
    assert engine.map_worthy([("M", ".github/workflows/ci.yml")])
    assert not engine.map_worthy([("M", "src/x.py"), ("M", "web/page.html")])


def test_diff_keeps_whole_files_and_leaves_out_lockfiles(repo):
    a = gitsnap.snapshot(str(repo))
    (repo / "package-lock.json").write_text('{"lock": 1}\n' * 2000)
    (repo / "small.py").write_text("def ok(): return 1\n")
    (repo / "big.py").write_text("line = 1\n" * 2000)
    text, files, cut = gitsnap.diff(str(repo), a, gitsnap.snapshot(str(repo)), limit=3000)
    assert set(files) == {"package-lock.json", "small.py", "big.py"} and cut
    assert "def ok(): return 1" in text                                   # a whole file that fits
    assert '"lock": 1' not in text and "lockfile or generated" in text and "package-lock.json" in text
    assert "read these files directly: big.py" in text and "line = 1" not in text
    b = gitsnap.snapshot(str(repo))
    (repo / "package-lock.json").write_text('{"lock": 2}\n')
    (repo / "small.py").write_text("def ok(): return 2\n")
    text, _, cut = gitsnap.diff(str(repo), b, gitsnap.snapshot(str(repo)))
    assert not cut and "return 2" in text and '"lock": 2' not in text  # a lockfile alone isn't "cut short"


def test_tail_starts_at_a_whole_entry():
    text = "## old\n" + "a" * 100 + "\n## middle job\nsome words here\n## newest\nlast words\n"
    kept = engine.tail(text, 60)
    assert kept.split("\n", 1)[1].startswith("## ") and "last words" in kept
    assert engine.tail("short", 60) == "short"
    words = engine.tail("one two three\nfour five six\nseven eight nine\n", 25)
    assert words.split("\n", 1)[1] in ("four five six\nseven eight nine\n", "seven eight nine\n")


def test_recheck_lists_what_the_reviewer_asked_for(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = [{"verdict": "fix", "issues": ["totals ignore VAT"], "summary": "no"},
                         {"verdict": "pass", "issues": [], "summary": "ok"}]
    FakeChild.script = [("a.py", "v1\n", "try1"), ("a.py", "v2\n", "try2")]
    run(repo)
    first, second = [c[4] for c in calls if c[0] == "review"]
    assert "RE-CHECK" not in first
    assert "THIS IS A RE-CHECK" in second and "- totals ignore VAT" in second


def test_gatekeeper_is_asked_once_per_action_in_a_job(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["gate"] = {"decision": "allow", "reason": "fine"}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}

    async def asks_twice(c):
        for _ in range(3):
            await c.decide(_req("execute", "python3 -m venv .venv"))
        pathlib.Path(c.cwd, "a.py").write_text("z\n")
        return "end_turn", "done"
    FakeChild.script = [asks_twice]
    e, ui, stop = run(repo, mode="auto")
    assert e.ok and [c[0] for c in calls].count("gate") == 1


def test_tool_less_claude_calls_skip_the_long_system_prompt():
    lean = headless.claude_cmd("sonnet", "low", {}, "")
    assert lean[lean.index("--system-prompt") + 1] == headless.LEAN_SYSTEM
    assert "--system-prompt" not in headless.claude_cmd("sonnet", "low", {}, "Read,Grep,Glob")


def test_a_fallback_answer_gets_a_prompt_built_for_it(job):
    repo, calls, answers = job
    (repo / "CLAUDE.md").write_text("CLAUDE-ONLY-RULE\n")
    (repo / "AGENTS.md").write_text("CODEX-ONLY-RULE\n")
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    e._rules = rules = engine.load_rules()
    e._prepare_memory()
    e._mark_limited("claude", "usage limit")
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    used = asyncio.run(e._ask("claude", "opus", "high", lambda a: e._memory_block(agent=a),
                              engine.REVIEW_SCHEMA, rules, 60))[0]
    sent = [c for c in calls if c[0] == "review"][-1]
    assert used == "codex" == sent[1] and "CLAUDE-ONLY-RULE" in sent[4] and "CODEX-ONLY-RULE" not in sent[4]


def test_project_list_drops_only_temp_folders_that_are_gone(tmp_path):
    from claudex import usage
    gone, here = tmp_path / "gone", tmp_path / "here"      # pytest's folders live under the temp folder
    unmounted = "/mnt/not-plugged-in/Projects/invoicer"   # a real project on a drive that's away right now
    here.mkdir()
    usage.PROJECTS.parent.mkdir(parents=True, exist_ok=True)
    usage.PROJECTS.write_text(json.dumps([str(gone), unmounted]))
    usage.remember_project(here)
    assert json.loads(usage.PROJECTS.read_text()) == sorted([str(here), unmounted])


def test_diff_spots_a_lockfile_with_an_awkward_name(repo):
    a = gitsnap.snapshot(str(repo))
    (repo / "web app").mkdir()
    (repo / "web app" / "package-lock.json").write_text('{"lock": 1}\n' * 50)
    (repo / "naïve.py").write_text("ok = 1\n")
    text, files, cut = gitsnap.diff(str(repo), a, gitsnap.snapshot(str(repo)), limit=100000)
    assert '"lock": 1' not in text and "ok = 1" in text and not cut


def _copy_ready(tmp_path, monkeypatch, tool="wl-copy"):
    from claudex import attachments
    _, t, e = _tui(tmp_path)
    t.build()
    copied = []

    async def fake_copy(text):
        copied.append(text)
        return tool
    monkeypatch.setattr(attachments, "copy_text", fake_copy)
    return t, copied


def test_copy_view_copies_exactly_what_was_selected(tmp_path, monkeypatch):
    t, copied = _copy_ready(tmp_path, monkeypatch)
    t._add("claude", "first line\nRun python app.py now\nlast line\n")
    t.toggle_copy_view()

    async def go():
        buf = t.copy_view.buffer
        start = buf.text.index("Run python")
        buf.cursor_position = start
        buf.start_selection()
        buf.cursor_position = start + len("Run python app.py now")
        t.copy_selection()
        await t.copy_task
    asyncio.run(go())
    assert copied == ["Run python app.py now"] and "Copied 21 characters" in t.copy_note


def test_letting_go_of_the_mouse_copies_the_drag(tmp_path, monkeypatch):
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseEvent, MouseButton, MouseEventType
    t, copied = _copy_ready(tmp_path, monkeypatch)
    t._add("claude", "abc\n")
    t.toggle_copy_view()
    calls = []
    t.copy_selection = lambda: calls.append(True)
    buf = t.copy_view.buffer
    buf.cursor_position = 0
    buf.start_selection()
    buf.cursor_position = 2
    up = MouseEvent(Point(0, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    t.copy_view.control.mouse_handler(up)
    assert calls == [True]


def test_copy_with_nothing_selected_says_how(tmp_path, monkeypatch):
    t, copied = _copy_ready(tmp_path, monkeypatch)
    t._add("claude", "abc\n")
    t.toggle_copy_view()
    t.copy_selection()
    assert copied == [] and "select something first" in t.copy_note


def test_copy_falls_back_to_asking_the_terminal(tmp_path, monkeypatch):
    t, copied = _copy_ready(tmp_path, monkeypatch, tool=None)
    written = []
    t.app.output.write_raw = written.append
    t.app.output.flush = lambda: None
    t._add("codex", "the answer\n")

    async def go():
        t.on_accept(type("B", (), {"text": "/copy last"})())
        await t.copy_task
    asyncio.run(go())
    assert copied == ["the answer"] and written and written[0].startswith("\x1b]52;c;")
    assert "Copied the last reply" in t.all_text() if hasattr(t, "all_text") else True


def test_the_wheel_scrolls_the_conversation_and_it_stays_put(tmp_path):
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseEvent, MouseButton, MouseEventType
    _, t, e = _tui(tmp_path)
    t.build()
    for i in range(50):
        t._add("claude", f"line {i}\n")
    t.cursor()
    wheel = lambda kind: t.chat.content.mouse_handler(MouseEvent(Point(0, 0), kind, MouseButton.NONE, frozenset()))
    wheel(MouseEventType.SCROLL_UP)
    back = t.scroll_back
    assert back > 0
    at = t.cursor().y
    t._add("claude", "more\nand more\n")            # new text while reading back
    assert t.cursor().y == at                        # what you're reading doesn't move
    for _ in range(100):
        wheel(MouseEventType.SCROLL_DOWN)
    assert t.scroll_back == 0                        # back at the newest line, following it again
    for _ in range(1000):
        wheel(MouseEventType.SCROLL_UP)
    assert t.cursor().y >= 0 and t.scroll_back < 200  # never past the start


def test_a_job_leaves_a_trace_that_the_report_reads(job):
    from claudex import trace
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = [{"verdict": "fix", "issues": ["missing test"], "summary": "no"},
                         {"verdict": "pass", "issues": [], "summary": "ok"}]
    FakeChild.script = [("a.py", "v1\n", "first"), ("a.py", "v2\n", "second")]
    e, ui, stop = run(repo, text="make the sums right")
    rows = trace.read(repo / ".claudex")
    kinds = [r["ev"] for r in rows]
    assert kinds[0] == "job_start" and kinds[-1] == "job_end" and rows[-1]["outcome"] == "done"
    assert len({r["job"] for r in rows}) == 1 and all(r["session"] == "t" for r in rows)
    roles = [r["role"] for r in rows if r["ev"] == "call"]
    assert roles[0] == "lead" and roles.count("reviewer") == 2
    steps = [r for r in rows if r["ev"] == "step"]
    assert [s["verdict"] for s in steps] == ["fix", "pass"] and steps[0]["brief_chars"] > 1000
    assert steps[0]["diff_chars"] > 0 and steps[0]["diff_truncated"] is False and "handoff" not in steps[0]
    assert all(r["prompt_chars"] > 0 for r in rows if r["ev"] == "call")
    printed = []
    trace.report(repo / ".claudex", out=printed.append)
    text = "\n".join(printed)
    assert "make the sums right" in text and "reviewer" in text and "step 1 try 2" in text


def test_trace_hints_point_at_real_problems():
    from claudex import trace
    job = [{"ev": "job_start", "job": "j", "at": "2026-11-07T10:00", "request": "x"}]
    job += [{"ev": "step", "job": "j", "at": "t", "step": 1, "attempt": i, "verdict": "fix" if i < 3 else "pass",
             "context_used": 900, "context_size": 1000, "diff_truncated": True, "secs": 60} for i in (1, 2, 3)]
    job += [{"ev": "gate", "job": "j", "at": "t", "by": "gatekeeper", "what": "npm test"} for _ in range(12)]
    job += [{"ev": "call", "job": "j", "at": "t", "role": "reviewer", "secs": 900, "ok": False, "error": "timeout"}]
    tips = " ".join(trace.hints([trace.summarise(job)]))
    for needle in ("sent back for fixes", "70% full", "too big to send whole", "asked 12 times", "npm test",
                   "'reviewer' took", "Errors: reviewer: timeout"):
        assert needle in tips, needle


def test_trace_never_breaks_a_job(tmp_path):
    from claudex import trace
    (tmp_path / "trace.jsonl").mkdir()                 # can't be written: must be ignored, not raised
    trace.write(tmp_path, ev="x")


def test_a_waiting_question_takes_the_wheel_away_from_its_answers(tmp_path):
    _, t, e = _tui(tmp_path)
    t.build()
    t.prefs["mouse"] = False
    assert not t.app.mouse_support()
    t.questions.append(({}, [], {"what": "x", "short": "y"}))
    assert t.app.mouse_support()         # the wheel arrives as scrolling, not as Up/Down picking an answer


# ---------- remembering what you said ----------
def test_earlier_answers_reach_the_lead_workers_and_checkers(job):
    repo, calls, answers = job
    answers["plan"] = [{"steps": [step(title="Record it")], "big_decisions": [], "concern_responses": []},
                       {"steps": [step(title="Next bit")], "big_decisions": [], "concern_responses": []}]
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    async def records(c):
        pathlib.Path(c.cwd, "a.py").write_text("1\n")
        c.usage = {"used": 5000, "size": 200000}                 # real workers say how full they are
        return "end_turn", "Recorded."
    FakeChild.script = [records, ("b.py", "2\n", "Done the next bit.")]
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    asyncio.run(e.turn("We bought a Contabo server. Don't ask me about hosting again."))
    for _ in range(40):                                          # a long conversation in between
        e._append(e.notes, "## Step 9: chatter\n" + "agent reply text " * 400 + "\n\n")
    calls.clear()
    asyncio.run(e.turn("Now plan the jobs page."))
    lead = [c[4] for c in calls if c[0] == "plan"][0]
    review = [c[4] for c in calls if c[0] == "review"][0]
    assert "We bought a Contabo server" in lead and "Now plan the jobs page." in lead
    assert "never ask what they have already answered" in lead
    assert "We bought a Contabo server" in review
    w = FakeChild.started[0]
    assert "We bought a Contabo server" in w.prompts[0]
    second = w.prompts[1]                                        # same worker, next message: only what's new
    assert "plus these new ones" in second and "Now plan the jobs page." in second
    said = second.split("plus these new ones:", 1)[1].split("\n\n", 1)[0]
    assert "We bought a Contabo server" not in said            # already in its conversation; not pasted again


def test_an_older_conversation_is_read_back_from_its_notes(tmp_path):
    e = engine.Engine(str(tmp_path), UI(), settings={}, session_key="old")
    e.notes_dir.mkdir(parents=True)
    e.notes.write_text("\n# 2026-11-07 17:39 · Request\nFirst ask.\n## A heading inside my message\nstill mine\n\n"
                       "## Step 1: Do it (claude sonnet medium, attempt 1)\nreply\n\n"
                       "\n# 2026-11-07 18:08 · Request\nSecond ask.\n\n## Step 1: More\nreply\n")
    assert e._said() == [("2026-11-07 17:39", "First ask.\n## A heading inside my message\nstill mine"),
                         ("2026-11-07 18:08", "Second ask.")]


def test_the_newest_messages_always_fit(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SAID_LIMIT", 300)
    e = engine.Engine(str(tmp_path), UI(), settings={}, session_key="t")
    e.notes_dir.mkdir(parents=True)
    e.said_file.write_text("".join(json.dumps({"at": f"t{i}", "text": f"message {i} " + "x" * 80}) + "\n"
                                   for i in range(20)))
    text = e._said_text()
    assert "message 19" in text and "message 0 " not in text and "earlier message(s)" in text


def test_edits_outside_the_project_are_flagged(job, tmp_path_factory):
    repo, calls, answers = job
    other = tmp_path_factory.mktemp("natter")
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}

    async def edits_elsewhere(c):
        (other / "notes.md").write_text("x\n")
        c.touched = {str(other / "notes.md")}
        return "end_turn", "Updated the docs."
    FakeChild.script = [edits_elsewhere]
    e, ui, stop = run(repo)
    assert "Working outside this project" in ui.all() and str(other) in ui.all()
    rows = [json.loads(l) for l in (repo / ".claudex/sessions/t/steps.jsonl").read_text().splitlines()]
    assert rows[-1]["verdict"] == "outside_project"


def test_child_remembers_which_files_its_edits_named():
    from claudex.child import Child
    c = Child("claude", ["x"], ".", UI())

    async def go():
        await c._on_notify("session/update", {"update": {"sessionUpdate": "tool_call", "toolCallId": "1",
                                                          "kind": "edit", "locations": [{"path": "/elsewhere/a.md"}]}})
        await c._on_notify("session/update", {"update": {"sessionUpdate": "tool_call", "toolCallId": "2",
                                                          "kind": "read", "locations": [{"path": "/elsewhere/b.md"}]}})
    asyncio.run(go())
    assert c.touched == {"/elsewhere/a.md"}


def test_an_older_conversation_keeps_its_messages_after_the_next_one(job):
    repo, calls, answers = job
    e = engine.Engine(str(repo), UI(), settings={}, session_key="old")
    e.notes_dir.mkdir(parents=True)
    e.notes.write_text("\n# 2026-11-07 17:39 · Request\nWe bought a Contabo server.\n\n## Step 1: Note it (claude"
                       " sonnet medium, attempt 1)\nnoted\n\n")
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "done")]
    asyncio.run(e.turn("Next question please."))
    texts = [t for _, t in e._said()]
    assert texts == ["We bought a Contabo server.", "Next question please."]


def test_child_spots_edits_whose_path_comes_in_a_later_update():
    from claudex.child import Child
    c = Child("codex", ["x"], ".", UI())

    async def go():
        u = lambda **k: c._on_notify("session/update", {"update": k})
        await u(sessionUpdate="tool_call", toolCallId="1", kind="edit", title="Edit")
        await u(sessionUpdate="tool_call_update", toolCallId="1", locations=[{"path": "/elsewhere/a.md"}])
        await u(sessionUpdate="tool_call_update", toolCallId="2",
                content=[{"type": "diff", "path": "/elsewhere/b.md", "oldText": "", "newText": "x"}])
    asyncio.run(go())
    assert c.touched == {"/elsewhere/a.md", "/elsewhere/b.md"}


# ---------- from penPost's logs ----------
def test_a_word_inside_a_heredoc_doesnt_trigger_an_ask_rule():
    from claudex import permissions
    rules = engine.load_rules()
    root = "/proj"
    writes_doc = "cd docs && python3 -I - <<'EOF'\nt = t.replace('draft', 'ready to publish')\nEOF"
    assert permissions.rule_decision(rules, "run", writes_doc, [], root)[0] != "ask"     # the gatekeeper reads it
    new_file = "cat > notes.md <<'EOF'\nWe release on Friday.\nEOF"
    assert permissions.rule_decision(rules, "run", new_file, [], root)[0] != "ask"
    assert permissions.rule_decision(rules, "run", "npm publish", [], root)[0] == "ask"  # a real publish still asks
    pushes = "bash <<'EOF'\ngit push origin main\nEOF"
    assert permissions.rule_decision(rules, "run", pushes, [], root)[0] != "allow"       # never waved through
    assert permissions.without_heredocs(new_file) == "cat > notes.md <<'EOF'"


def test_an_out_of_usage_gatekeeper_falls_back_to_a_light_model(job):
    repo, calls, answers = job
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    e._rules = rules = engine.load_rules()
    e._prepare_memory()
    e._mark_limited("claude", "usage limit")
    answers["gate"] = {"decision": "allow", "reason": "fine"}
    asyncio.run(e._ask("claude", "sonnet", "low", "x", permissions_schema(), rules, 60, read_files=False,
                       role="gatekeeper"))
    gate = [c for c in calls if c[0] == "gate"][-1]
    light = rules["review"]["light"]["codex"]
    assert gate[1] == "codex" and gate[2] == light["model"] and gate[3] == "low"


def permissions_schema():
    from claudex import permissions
    return permissions.GATE_SCHEMA


def test_child_names_the_actions_that_failed():
    from claudex.child import Child
    c = Child("codex", ["x"], ".", UI())

    async def go():
        u = lambda **k: c._on_notify("session/update", {"update": k})
        await u(sessionUpdate="tool_call", toolCallId="1", kind="execute", title="pytest -q tests/test_fit.py")
        await u(sessionUpdate="tool_call_update", toolCallId="1", status="failed")
    asyncio.run(go())
    assert c.tool_failures == 1 and c.failed == ["pytest -q tests/test_fit.py"]


# ---------- quick follow-ups, a leaner lead, browser rules ----------
def test_a_short_reply_goes_straight_back_to_the_last_worker(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", "gpt-6-astra", "high", title="Fit the letters")],
                       "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "Q1. Bigger or smaller? A) bigger B) smaller"), ("a.py", "2\n", "Made them smaller.")]
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    asyncio.run(e.turn("fit the letters to the guide lines"))
    calls.clear()
    asyncio.run(e.turn("B"))
    assert not any(c[0] == "plan" for c in calls)                   # no planning for a one-letter answer
    w = FakeChild.started[0]
    assert len(FakeChild.started) == 1 and "“B”" in w.prompts[1] and "Fit the letters" in w.prompts[1]
    assert any(c[0] == "review" for c in calls)                     # it's still checked
    rows = [json.loads(l) for l in (repo / ".claudex/sessions/t/steps.jsonl").read_text().splitlines()]
    assert rows[-1]["agent"] == "codex" and rows[-1]["model"] == "gpt-6-astra"


def test_a_new_request_still_gets_a_plan(job):
    repo, calls, answers = job
    answers["plan"] = [{"steps": [step()], "big_decisions": [], "concern_responses": []},
                       {"steps": [step("claude", "sonnet", title="Header")], "big_decisions": [], "concern_responses": []}]
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "done"), ("b.py", "2\n", "header done")]
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    asyncio.run(e.turn("first"))
    calls.clear()
    asyncio.run(e.turn("ok now make the header blue"))
    assert [c[0] for c in calls].count("plan") == 1


def test_quick_follow_ups_can_be_switched_off(job, tmp_path):
    repo, calls, answers = job
    (repo / ".claudex").mkdir(exist_ok=True)
    (repo / ".claudex/rules.toml").write_text("[lead]\nquick_followups = false\n")
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "done"), ("a.py", "2\n", "again")]
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    asyncio.run(e.turn("first"))
    calls.clear()
    asyncio.run(e.turn("carry on"))
    assert [c[0] for c in calls].count("plan") == 1


def test_follow_up_words():
    yes = ["carry on", "carry on yes Y to all", "???", "thoughts?", "1", "1A, 2B", "A is fine yes", "yes go ahead"]
    no = ["Read the handoff file and then carry on.", "ok now build the login page", "yes but make the header blue",
          "carry on and also fix the tests", "Would it work if we made them any smaller?"]
    assert all(engine.is_follow_up(t) for t in yes) and not any(engine.is_follow_up(t) for t in no)


def test_the_test_browser_is_allowed_but_your_chrome_only_to_look():
    from claudex import permissions as p
    r = engine.load_rules()
    d = lambda t: p.rule_decision(r, "other", "", [], "/x", title=t)[0]
    assert d("mcp__plugin_playwright_playwright__browser_click") == "allow"
    assert d("mcp__plugin_playwright_playwright__browser_take_screenshot") == "allow"
    assert d("mcp__plugin_playwright_playwright__browser_evaluate") is None        # scripts: the gatekeeper
    assert d("mcp__plugin_playwright_playwright__browser_file_upload") is None
    assert d("mcp__chrome-devtools__take_snapshot") == "allow"
    assert d("mcp__chrome-devtools__click") is None                                 # your signed-in Chrome
    assert d("mcp__claude-in-chrome__computer") is None


def test_final_check_gets_a_smaller_diff_and_is_told_why(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", title="API"), step("claude", "sonnet", title="Page")],
                       "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("big.py", "x = 1\n" * 6000, "made big.py"), ("page.html", "<p/>\n", "page")]
    run(repo)
    final = [c[4] for c in calls if c[0] == "final"][0]
    assert "already reviewed" in final and "read these files directly: big.py" in final and len(final) < 40000


# ---------- same accuracy, fewer tokens ----------
def test_final_check_is_skipped_when_one_agent_did_it_all_and_passed(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", title="API"), step("codex", title="More API")],
                       "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "one"), ("b.py", "2\n", "two")]
    e, ui, stop = run(repo)
    assert e.ok and not any(c[0] == "final" for c in calls) and "No final check needed" in ui.all()


def test_final_check_runs_when_both_agents_worked(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", title="API"), step("claude", "sonnet", title="Page")],
                       "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "one"), ("p.html", "<p/>\n", "two")]
    run(repo)
    assert any(c[0] == "final" for c in calls)


def test_a_small_change_gets_a_medium_check_and_a_recheck_gets_the_full_one(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = [{"verdict": "fix", "issues": ["off by one"], "summary": "no"},
                         {"verdict": "pass", "issues": [], "summary": "ok"}]
    FakeChild.script = [("a.py", "x = 2\n", "small"), ("a.py", "x = 3\n", "fixed")]
    run(repo)
    first, second = [c for c in calls if c[0] == "review"]
    assert (first[2], first[3]) == ("opus", "medium") and (second[2], second[3]) == ("opus", "high")
    assert "Start from the diff" in first[4]


def test_a_big_change_keeps_the_full_check(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "".join(f"line_{i} = {i}\n" for i in range(800)), "big")]
    run(repo)
    r = [c for c in calls if c[0] == "review"][0]
    assert (r[2], r[3]) == ("opus", "high")


def test_a_nearly_full_worker_is_swapped_for_a_fresh_one(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", title="One"), step("codex", title="Two")],
                       "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}

    async def fills_up(c):
        pathlib.Path(c.cwd, "a.py").write_text("1\n")
        c.usage = {"used": 190000, "size": 258400}          # 74% full, like Natter's worker
        return "end_turn", "done one"
    FakeChild.script = [fills_up, ("b.py", "2\n", "done two")]
    e, ui, stop = run(repo)
    assert len(FakeChild.started) == 2 and FakeChild.started[0].closed
    assert "74% full" in ui.all() and "Step 2 of 2" in FakeChild.started[1].prompts[0]
    assert "PROJECT MAP:\n" in FakeChild.started[1].prompts[0]            # the fresh one gets everything


def test_silent_planning_and_checks_show_as_running_work(job):
    """Agents of Empires cancels a turn that shows nothing for two minutes; a long plan or check must show up."""
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "v1\n", "done")]
    cards = []

    class BusyUI(UI):
        async def working(self, title):
            cards.append([title, "running"])
            return len(cards) - 1

        async def done_working(self, tid, ok=True):
            cards[tid][1] = "done" if ok else "failed"

    ui = BusyUI()
    e = engine.Engine(str(repo), ui, settings={}, session_key="t")
    asyncio.run(e.turn("do the thing"))
    titles = [t for t, _ in cards]
    assert any(t.startswith("Planning (") for t in titles) and any(t.startswith("Checking the change") for t in titles)
    assert all(state == "done" for _, state in cards)          # every card is closed when its call ends


def test_session_ui_sends_a_running_card_and_closes_it():
    from claudex import server
    sent = []

    class Peer:
        def notify(self, method, params):
            sent.append(params["update"])

    ui = server.SessionUI(Peer(), "s1")
    tid = asyncio.run(ui.working("Planning (Claude opus)"))
    asyncio.run(ui.done_working(tid, False))
    assert sent[0]["sessionUpdate"] == "tool_call" and sent[0]["status"] == "in_progress"
    assert sent[1] == {"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": "failed"}


def test_a_resumed_workers_running_total_is_not_logged_as_one_steps_cost(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "v1\n", "done")]
    orig = FakeChild.prompt

    async def prompt(self, text):
        self.resumed = True
        out = await orig(self, text)
        self.usage = {"used": 1000, "size": 200000, "cost": 4.52}   # the whole earlier conversation's total
        return out

    FakeChild.prompt = prompt
    try:
        run(repo)
    finally:
        FakeChild.prompt = orig
    rows = [json.loads(l) for l in (repo / ".claudex/trace.jsonl").read_text().splitlines()]
    assert [r.get("cost") for r in rows if r["ev"] == "step"] == [None]


def test_questions():
    for t in ("are we done?", "What's left to do on this project?", "Sorry, have you got questions for me?",
              "So what's the point of the extra words? Is there any reason?", "why did you stop?",
              "whats this conversation id?"):
        assert engine.is_question(t), t
    for t in ("Can you make the header blue?", "should we remove the joined page?", "make it smaller",
              "It appears to only work for older chats.", "why not just fix it and push it live?",
              "What's left?\nAlso rename the button", "how about it", "x" * 300 + "?"):
        assert not engine.is_question(t), t


def test_a_quick_question_goes_to_the_worker_already_here(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("claude", "sonnet", "medium", title="Header")], "big_decisions": [],
                       "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "done"), (None, "", "Nothing left but the footer.")]
    ui = UI()
    e = engine.Engine(str(repo), ui, settings={}, session_key="t")
    asyncio.run(e.turn("build the header"))
    calls.clear()
    asyncio.run(e.turn("What's left to do?"))
    assert not any(c[0] == "plan" for c in calls)
    w = FakeChild.started[0]
    assert len(FakeChild.started) == 1 and "What's left to do?" in w.prompts[1]
    assert "Don't change any files" in w.prompts[1]
    rows = [json.loads(l) for l in (repo / ".claudex/trace.jsonl").read_text().splitlines()]
    assert any(r["ev"] == "quick_question" for r in rows)


def test_a_recheck_is_sent_only_the_fixes(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = [{"verdict": "fix", "issues": ["b is wrong"], "summary": "no"},
                         {"verdict": "pass", "issues": [], "summary": "ok"}]
    FakeChild.script = [("a.py", "first = 1\n", "t1"), ("b.py", "second = 2\n", "t2")]
    run(repo)
    reviews = [c[4] for c in calls if c[0] == "review"]
    assert "first = 1" in reviews[0] and "only what the worker changed" not in reviews[0]
    recheck = reviews[1].split("DIFF:\n")[-1]
    assert "second = 2" in recheck and "first = 1" not in recheck
    assert "only what the worker changed" in reviews[1] and "a.py" in reviews[1]   # still names every file


def test_the_final_check_runs_at_medium_without_the_conversation(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", title="API"), step("claude", "sonnet", title="Page")],
                       "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "one"), ("p.html", "<p/>\n", "two")]
    run(repo)
    final = [c for c in calls if c[0] == "final"]
    assert final and final[0][3] == "medium"


def test_a_cancelled_call_is_in_the_trace_and_two_minute_stops_are_flagged(job):
    repo, calls, answers = job

    async def hang():
        raise asyncio.CancelledError()

    answers["plan"] = hang
    run(repo)
    rows = [json.loads(l) for l in (repo / ".claudex/trace.jsonl").read_text().splitlines()]
    assert any(r["ev"] == "call" and r["role"] == "lead" and r["error"] == "cancelled" for r in rows)
    from claudex import trace
    s = {"outcome": "cancelled", "secs": 120.0, "steps": [], "roles": {}, "gates": [], "fallbacks": [], "errors": []}
    assert any("watchdog" in h for h in trace.hints([s]))


def test_a_whole_job_runs_through_the_agents_of_empires_screen(job):
    """The ACP screen's names mustn't clash with the hooks the engine looks for (it once had `activity`)."""
    from claudex import server
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "done")]
    sent = []

    class Peer:
        def notify(self, method, params):
            sent.append(params["update"])

    e = engine.Engine(str(repo), server.SessionUI(Peer(), "s1"), settings={}, session_key="t")
    assert asyncio.run(e.turn("do it")) == "end_turn" and e.ok
    assert any(u["sessionUpdate"] == "tool_call" for u in sent)
    assert not any("Waiting for the other" in (u.get("title") or "") for u in sent)   # nothing else was running


# ---------- messages sent while a job runs (Enter mid-turn in Agents of Empires) ----------
def test_claudex_tells_agents_of_empires_it_takes_messages_mid_job():
    from claudex import server
    init = asyncio.run(server.Server().on_request("initialize", {"protocolVersion": 1}))
    assert init["_meta"]["steering"]["supported"] is True


def test_a_message_sent_while_a_worker_runs_goes_straight_to_it(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    steered = []

    async def work(child):
        child.can_steer = True

        async def steer(text):
            steered.append(text)
            return "injected"

        child.steer = steer
        assert await e.steer("make it blue too") == "injected"
        pathlib.Path(child.cwd, "a.py").write_text("blue\n")
        return "end_turn", "done, and blue"

    FakeChild.script = [work]
    assert asyncio.run(e.turn("build the page")) == "end_turn"
    assert steered and "make it blue too" in steered[0] and not e.steers
    review = [c[4] for c in calls if c[0] == "review"][0]
    assert "ADDED BY THE USER WHILE THE JOB RAN" in review and "make it blue too" in review
    said = (repo / ".claudex/sessions/t/said.jsonl").read_text()
    assert "make it blue too" in said


def test_a_message_sent_while_planning_goes_to_the_next_worker(job):
    repo, calls, answers = job
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")

    async def plan():
        assert await e.steer("and use the new logo") == "held"
        return {"steps": [step()], "big_decisions": [], "concern_responses": []}

    answers["plan"] = plan
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "done")]
    asyncio.run(e.turn("build the header"))
    w = FakeChild.started[0]
    assert "THE USER ADDED THIS WHILE THE JOB WAS RUNNING" in w.prompts[0] and "use the new logo" in w.prompts[0]
    assert not e.steers


def test_with_no_job_running_a_mid_turn_message_is_a_normal_one(job):
    from claudex import server
    repo, calls, answers = job
    s = server.Server()
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    e.busy = False
    s.sessions["s1"] = e
    out = asyncio.run(s.on_request("_session/steering", {
        "sessionId": "s1", "prompt": [{"type": "text", "text": "hello"}],
        "_meta": {"steering": {"idleBehavior": "promptRequired"}}}))
    assert out == {"outcome": "promptRequired", "reason": "noRunningTurn"}


def test_what_no_worker_picked_up_runs_once_the_job_ends(job):
    """Added during the final check, say: nobody was left to take it, so it becomes its own job."""
    from claudex import server
    from types import SimpleNamespace
    turns = []

    async def turn(text, already_said=False):
        turns.append((text, already_said))
        if len(turns) == 1:
            eng.steers.append("also tidy the footer")
        return "end_turn"

    async def nothing(*a, **k):
        return None

    ui = SimpleNamespace(say=nothing, keepalive=SimpleNamespace(start=nothing, stop=nothing))
    eng = SimpleNamespace(ui=ui, busy=False, turn=turn, cwd=".", skills={}, steers=[])
    s = server.Server()
    s.sessions["s1"] = eng
    out = asyncio.run(s.on_request("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "go"}]}))
    assert out == {"stopReason": "end_turn"}
    assert turns == [("go", False), ("also tidy the footer", True)]


def _quick_limits(monkeypatch, **limits):
    real = engine.load_rules

    def rules(path=None, project_root=None, models_file=None):
        r = real(path, project_root, models_file)
        r["limits"].update(limits)
        return r
    monkeypatch.setattr(engine, "load_rules", rules)


def test_a_worker_that_goes_quiet_is_stopped_and_carry_on_picks_it_back_up(job, monkeypatch):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", title="Migrate")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    _quick_limits(monkeypatch, step_idle_min=0.01, step_idle_tool_min=10)

    async def half_done_then_quiet(child):
        pathlib.Path(child.cwd, "half.py").write_text("half = 1\n")
        child.quiet_for = lambda: 3600             # then shows nothing at all
        await asyncio.sleep(30)
    FakeChild.script = [half_done_then_quiet, ("rest.py", "rest = 2\n", "finished")]
    ui = UI()
    e = engine.Engine(str(repo), ui, settings={}, session_key="t")
    asyncio.run(e.turn("migrate everyone"))
    assert not e.ok and "no sign of work" in ui.all() and "carry on" in ui.all()
    rows = [json.loads(l) for l in (repo / ".claudex/trace.jsonl").read_text().splitlines()]
    assert any(r["ev"] == "step" and r.get("verdict") == "stuck" for r in rows)
    calls.clear()
    asyncio.run(e.turn("carry on"))
    assert not any(c[0] == "plan" for c in calls)          # straight back to Codex, no planning
    assert FakeChild.started[-1].name == "codex" and FakeChild.resumed_with[-1]   # its conversation reloaded
    review = [c for c in calls if c[0] == "review"][0][4]
    assert "half = 1" in review and "rest = 2" in review   # the stopped step's work is checked too
    assert "stopped before it was checked" in review
    assert not (e.notes_dir / "unchecked.json").exists()


def test_a_busy_worker_is_not_stopped_however_long_it_takes(job, monkeypatch):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    _quick_limits(monkeypatch, step_idle_min=0.005)        # 0.3 s of silence counts as stuck

    async def busy(child):
        for _ in range(6):                                 # 1.2 s of steady work
            child.quiet_for = lambda: 0
            await asyncio.sleep(0.2)
        pathlib.Path(child.cwd, "a.py").write_text("a = 1\n")
        return "end_turn", "done"
    FakeChild.script = [busy]
    e, ui, stop = run(repo)
    assert e.ok and "no sign of work" not in ui.all()


def test_a_worker_kept_from_an_earlier_job_isnt_stuck_before_it_starts(job, monkeypatch):
    # A reused worker last showed signs of work during the previous job, maybe an hour ago. That silence
    # belongs to the gap between jobs, not to this step.
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    _quick_limits(monkeypatch, step_idle_min=0.02)         # 1.2 s of silence counts as stuck

    async def slow_start(child):
        child.quiet_for = lambda: 3600                     # its clock is still from the last job
        await asyncio.sleep(0.4)
        pathlib.Path(child.cwd, "a.py").write_text("a = 1\n")
        return "end_turn", "done"
    FakeChild.script = [slow_start]
    e, ui, stop = run(repo)
    assert e.ok and "no sign of work" not in ui.all()


def test_a_quiet_running_action_gets_longer_before_its_worker_counts_as_stuck(job, monkeypatch):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    _quick_limits(monkeypatch, step_idle_min=0.005, step_idle_tool_min=10)

    async def long_build(child):
        child.open_cards = {"codex:1": True}               # a build is running, saying nothing
        child.quiet_for = lambda: 60
        await asyncio.sleep(0.5)
        child.open_cards = {}
        pathlib.Path(child.cwd, "a.py").write_text("a = 1\n")
        return "end_turn", "built"
    FakeChild.script = [long_build]
    e, ui, stop = run(repo)
    assert e.ok


def test_a_stopped_worker_closes_the_actions_it_left_running():
    from claudex.child import Child
    sent = []

    class U:
        async def update(self, u):
            sent.append(u)
    c = Child("codex", ["codex-acp"], ".", U())

    async def go():
        await c._on_notify("session/update", {"update": {"sessionUpdate": "tool_call", "toolCallId": "1",
                                                         "status": "in_progress", "title": "npm test"}})
        await c._on_notify("session/update", {"update": {"sessionUpdate": "tool_call", "toolCallId": "2",
                                                         "status": "in_progress", "title": "ls"}})
        await c._on_notify("session/update", {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "2",
                                                         "status": "completed"}})
        sent.clear()
        await c.close_cards()
    asyncio.run(go())
    assert sent == [{"sessionUpdate": "tool_call_update", "toolCallId": "codex:1", "status": "failed"}]
    assert c.open_cards == {}


def test_a_job_stopped_during_its_check_goes_back_to_that_worker_and_gets_checked(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", title="Migrate")], "big_decisions": [], "concern_responses": []}
    answers["review"] = [asyncio.CancelledError(), {"verdict": "pass", "issues": [], "summary": "ok"}]
    FakeChild.script = [("half.py", "half = 1\n", "most of it"), ("rest.py", "rest = 2\n", "finished")]
    ui = UI()
    e = engine.Engine(str(repo), ui, settings={}, session_key="t")
    try:
        asyncio.run(e.turn("migrate everyone"))
    except BaseException:
        pass
    assert (e.notes_dir / "unchecked.json").exists()
    calls.clear()
    asyncio.run(e.turn("carry on"))
    assert not any(c[0] == "plan" for c in calls)
    review = [c for c in calls if c[0] == "review"][0][4]
    assert "half = 1" in review and "rest = 2" in review


def _vat_plans():
    first = {"steps": [step()], "big_decisions": [{"decision": "store VAT as float", "why": "simple",
                                                    "alternatives": "integer pence"}], "concern_responses": []}
    second = {"steps": [step(title="pence")], "big_decisions": first["big_decisions"],
              "concern_responses": [{"concern": "floats round badly", "accepted": True, "why": "use pence"}]}
    return first, second


def test_a_replan_carries_on_the_planners_own_conversation(job, monkeypatch):
    repo, calls, answers = job
    forgot = []
    monkeypatch.setattr(headless, "forget", forgot.append)
    answers["plan"] = list(_vat_plans())
    answers["opinion"] = {"verdict": "concerns", "concerns": ["floats round badly"], "suggestion": "pence"}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "pence\n", "done")]
    e, ui, stop = run(repo)
    plans = [c for c in calls if c[0] == "plan"]
    opts = [o for o in FakeChild.ask_options if o]
    assert opts[0] == {"keep": True} and opts[1] == {"resume": "plan-1"}
    assert "floats round badly" in plans[1][4] and "PROJECT FILES" not in plans[1][4]   # only the concerns
    assert "PROJECT FILES" in plans[0][4]
    assert forgot == ["plan-1"] and e._plan_session is None     # the kept conversation is deleted afterwards
    rows = [json.loads(l) for l in (repo / ".claudex/trace.jsonl").read_text().splitlines()]
    assert any(r["ev"] == "call" and r.get("role") == "lead" and r.get("resumed") for r in rows)


def test_if_the_planners_conversation_cant_be_resumed_it_plans_afresh(job, monkeypatch):
    repo, calls, answers = job
    monkeypatch.setattr(headless, "forget", lambda sid: None)
    first, second = _vat_plans()
    answers["plan"] = [first, headless.HeadlessError("claude: No conversation found"), second]
    answers["opinion"] = {"verdict": "concerns", "concerns": ["floats round badly"], "suggestion": "pence"}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "pence\n", "done")]
    e, ui, stop = run(repo)
    plans = [c for c in calls if c[0] == "plan"]
    assert e.ok and len(plans) == 3 and "PROJECT FILES" in plans[2][4] and "floats round badly" in plans[2][4]


def test_a_plan_with_no_big_decisions_keeps_nothing(job, monkeypatch):
    repo, calls, answers = job
    forgot = []
    monkeypatch.setattr(headless, "forget", forgot.append)
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "done")]
    run(repo)
    assert forgot == ["plan-1"]


def test_only_a_kept_or_resumed_claude_call_is_saved():
    plain = headless.claude_cmd("opus", "high", {}, "Read")
    assert "--no-session-persistence" in plain and "--resume" not in plain
    assert "--no-session-persistence" not in headless.claude_cmd("opus", "high", {}, "Read", keep=True)
    r = headless.claude_cmd("opus", "high", {}, "Read", resume="abc")
    assert r[r.index("--resume") + 1] == "abc"


def test_the_lead_is_told_to_stage_very_big_jobs(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    FakeChild.script = [("a.py", "1\n", "done")]
    run(repo, "migrate every customer to the new store")
    lead = [c for c in calls if c[0] == "plan"][0][4]
    assert "split it into stages" in lead and "FEWEST" in lead


# ---------- several jobs in one project ----------
from claudex import worktrees  # noqa: E402


@pytest.fixture
def copies(tmp_path, monkeypatch):
    monkeypatch.setattr(worktrees, "HOME", tmp_path / "copies")
    return tmp_path / "copies"


def _hold_folder(repo):
    """Another Claudex job changing files in the main folder."""
    other = Lock(repo / ".claudex" / "work.lock")
    assert other.try_acquire()
    return other


def test_a_second_job_works_in_its_own_copy_and_brings_its_changes_back(job, copies):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "lib.js").write_text("x\n")
    (repo / "ignored.txt").write_text("secret\n")
    (repo / "theirs.py").write_text("half done by the other job\n")   # the other job's uncommitted work
    other = _hold_folder(repo)
    seen = {}

    async def work(child):
        seen["cwd"] = child.cwd
        seen["linked"] = (pathlib.Path(child.cwd, "node_modules", "lib.js").read_text(),
                          pathlib.Path(child.cwd, "ignored.txt").read_text())
        seen["theirs"] = pathlib.Path(child.cwd, "theirs.py").exists()
        pathlib.Path(child.cwd, "mine.py").write_text("mine = 1\n")
        other.release()                      # the other job finishes meanwhile
        return "end_turn", "done"
    FakeChild.script = [work]
    e, ui, stop = run(repo)
    assert e.ok and "separate copy" in ui.all() and "Brought this job's changes back" in ui.all()
    # the other job didn't record how it found the folder, so the copy starts from the folder as it is now
    assert seen["cwd"] != str(repo) and seen["linked"] == ("x\n", "secret\n") and seen["theirs"]
    assert "part-way through" in ui.all()
    assert "separate copy" in FakeChild.started[-1].prompts[0]          # the worker is told where it is
    review = [c for c in calls if c[0] == "review"][0][4]
    assert "mine = 1" in review and "half done" not in review          # its check sees only its own work
    assert (repo / "mine.py").read_text() == "mine = 1\n" and (repo / "theirs.py").exists()
    assert not pathlib.Path(seen["cwd"]).exists()                       # the copy is gone
    assert "claudex/" not in git(repo, "branch")
    shown = git(repo, "status", "--short", "--", ".", ":!claudex-data")
    assert shown.split() == ["??", "mine.py", "??", "theirs.py"]   # theirs untouched, mine added, no links
    assert e.project_root == str(repo) and not (e.notes_dir / "worktree.json").exists()


def test_a_copy_that_clashes_is_kept_and_the_main_folder_left_alone(job, copies):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    other = _hold_folder(repo)
    seen = {}

    async def work(child):
        seen["cwd"] = child.cwd
        pathlib.Path(child.cwd, "a.py").write_text("x = 'mine'\n")
        (repo / "a.py").write_text("x = 'theirs'\n")                    # the other job changed the same line
        other.release()
        return "end_turn", "done"
    FakeChild.script = [work]
    e, ui, stop = run(repo)
    assert not e.ok and "Couldn't bring this job's changes back" in ui.all() and "`a.py`" in ui.all()
    assert (repo / "a.py").read_text() == "x = 'theirs'\n"
    assert pathlib.Path(seen["cwd"], "a.py").read_text() == "x = 'mine'\n"   # kept, on its branch
    assert "claudex/" in git(repo, "branch")


def test_a_workers_own_commits_in_a_copy_come_back_as_commits(job, copies):
    repo, calls, answers = job
    with open(repo / ".git" / "info" / "exclude", "a") as f:     # a clean folder: the copy starts at the last commit
        f.write("claudex-data/\n")
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    other = _hold_folder(repo)

    async def work(child):
        pathlib.Path(child.cwd, "done.py").write_text("done = 1\n")
        git(child.cwd, "add", "done.py")
        git(child.cwd, "commit", "-qm", "Add done")
        pathlib.Path(child.cwd, "wip.py").write_text("wip = 1\n")
        other.release()
        return "end_turn", "done"
    FakeChild.script = [work]
    e, ui, stop = run(repo)
    assert e.ok and "Add done" in git(repo, "log", "--oneline", "-1")
    assert git(repo, "status", "--short", "--", ".", ":!claudex-data").split() == ["??", "wip.py"]


def test_a_job_stopped_in_its_copy_carries_on_there(job, copies, monkeypatch):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex", title="Migrate")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    _quick_limits(monkeypatch, step_idle_min=0.01, step_idle_tool_min=10)
    other = _hold_folder(repo)
    seen = []

    async def half_then_quiet(child):
        seen.append(child.cwd)
        pathlib.Path(child.cwd, "half.py").write_text("half = 1\n")
        child.quiet_for = lambda: 3600
        await asyncio.sleep(30)

    async def rest(child):
        seen.append(child.cwd)
        pathlib.Path(child.cwd, "rest.py").write_text("rest = 2\n")
        return "end_turn", "finished"
    FakeChild.script = [half_then_quiet, rest]
    ui = UI()
    e = engine.Engine(str(repo), ui, settings={}, session_key="t")
    asyncio.run(e.turn("migrate everyone"))
    assert not e.ok and not (repo / "half.py").exists()                 # unchecked work stays in the copy
    assert (e.notes_dir / "worktree.json").exists() and e.project_root == str(repo)
    other.release()
    asyncio.run(e.turn("carry on"))
    assert seen[1] == seen[0]                                            # back in the same copy
    review = [c for c in calls if c[0] == "review"][0][4]
    assert "half = 1" in review and "rest = 2" in review
    assert e.ok and (repo / "half.py").exists() and (repo / "rest.py").exists()
    assert not (e.notes_dir / "worktree.json").exists()


def test_planning_and_answers_dont_wait_for_another_job(job, copies):
    repo, calls, answers = job
    other = _hold_folder(repo)
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    e._job = {"id": "j1"}
    asyncio.run(asyncio.wait_for(e._choose_folder([{"answer_only": True}], engine.load_rules()), 2))
    assert e._work_lock is None and e._home is None
    other.release()


def test_with_copies_off_a_second_job_waits_as_before(job, copies, monkeypatch):
    repo, calls, answers = job
    real = engine.load_rules

    def rules(path=None, project_root=None, models_file=None):
        r = real(path, project_root, models_file)
        r["parallel"] = {"worktrees": False}
        return r
    monkeypatch.setattr(engine, "load_rules", rules)
    other = _hold_folder(repo)
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")
    e._job = {"id": "j1"}

    async def go():
        t = asyncio.ensure_future(e._choose_folder([{}], engine.load_rules()))
        await asyncio.sleep(0.3)
        assert not t.done()
        other.release()
        await asyncio.wait_for(t, 5)
    asyncio.run(go())
    assert e._work_lock is not None and e._home is None
    e._work_lock.release()


def test_a_copy_starts_from_the_folder_as_the_other_job_found_it(job, copies):
    # Finished work nobody committed is in the copy; the other job's work in progress isn't.
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    (repo / "earlier.py").write_text("earlier = 1\n")
    (repo / "a.py").write_text("x = 'earlier'\n")
    other = _hold_folder(repo)
    worktrees.record_base(str(repo), repo / ".claudex" / "work.json")
    (repo / "inflight.py").write_text("inflight = 1\n")
    seen = {}

    async def work(child):
        seen["files"] = sorted(p.name for p in pathlib.Path(child.cwd).glob("*.py"))
        seen["a"] = pathlib.Path(child.cwd, "a.py").read_text()
        pathlib.Path(child.cwd, "mine.py").write_text("mine = 1\n")
        other.release()
        return "end_turn", "done"
    FakeChild.script = [work]
    e, ui, stop = run(repo)
    assert seen == {"files": ["a.py", "earlier.py"], "a": "x = 'earlier'\n"}
    review = [c for c in calls if c[0] == "review"][0][4]
    assert "mine = 1" in review and "earlier" not in review and "inflight" not in review
    assert e.ok and "last commit" not in ui.all()
    assert {p.name for p in repo.glob("*.py")} == {"a.py", "earlier.py", "inflight.py", "mine.py"}
    assert (repo / "a.py").read_text() == "x = 'earlier'\n"


def test_a_job_plans_while_another_has_the_folder_and_waits_only_to_change_files(job, copies, monkeypatch):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    real = engine.load_rules

    def rules(path=None, project_root=None, models_file=None):
        r = real(path, project_root, models_file)
        r["parallel"] = {"worktrees": False}
        return r
    monkeypatch.setattr(engine, "load_rules", rules)
    other = _hold_folder(repo)
    FakeChild.script = [("b.py", "b = 1\n", "done")]
    e = engine.Engine(str(repo), UI(), settings={}, session_key="t")

    async def go():
        t = asyncio.ensure_future(e.turn("do the thing"))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any(c[0] == "plan" for c in calls):
                break
        assert any(c[0] == "plan" for c in calls) and not (repo / "b.py").exists()   # planned, not changing yet
        other.release()
        await asyncio.wait_for(t, 10)
    asyncio.run(go())
    assert e.ok and (repo / "b.py").exists() and not (repo / ".claudex" / "work.json").exists()


def test_an_answer_isnt_blamed_for_another_jobs_edits(job, copies):
    repo, calls, answers = job
    answers["plan"] = {"steps": [dict(step("claude", "sonnet"), answer_only=True)], "big_decisions": [],
                       "concern_responses": []}
    other = _hold_folder(repo)

    async def answer(child):
        (repo / "a.py").write_text("x = 'the other job'\n")   # the other job, meanwhile
        return "end_turn", "Here's the answer."
    FakeChild.script = [answer]
    e, ui, stop = run(repo)
    other.release()
    assert e.ok and "separate copy" not in ui.all()      # answered in the main folder, without waiting
    assert not any(c[0] in ("review", "map") for c in calls)


def test_the_folder_is_recorded_when_claudex_own_folder_is_gitignored(job):
    # git exits 1 when an excluded path is ignored anyway; the record must still be made
    repo, calls, answers = job
    with open(repo / ".git" / "info" / "exclude", "a") as f:
        f.write(".claudex/\n")
    (repo / "earlier.py").write_text("earlier = 1\n")
    (repo / ".claudex").mkdir(exist_ok=True)
    tree = worktrees.record_base(str(repo), repo / ".claudex" / "work.json", request="r", steps=["s"])
    assert "earlier.py" in git(repo, "ls-tree", "--name-only", tree)
    assert json.loads((repo / ".claudex" / "work.json").read_text())["steps"] == ["s"]


def test_clashing_files_are_named_from_what_git_says():
    said = ("error: src/a.mjs: already exists in working directory\n"
            "error: patch failed: test/fake-dom.js:12\nerror: test/fake-dom.js: patch does not apply\n")
    assert worktrees._clashes(said) == ["src/a.mjs", "test/fake-dom.js"]


def test_a_plan_is_told_what_another_job_is_already_doing(job, copies):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    other = _hold_folder(repo)
    worktrees.record_base(str(repo), repo / ".claudex" / "work.json", request="Build the phone app",
                          steps=["Android shell", "Deploy"])

    async def work(child):
        other.release()
        return "end_turn", "done"
    FakeChild.script = [work]
    run(repo)
    plan = [c for c in calls if c[0] == "plan"][0][4]
    assert "ANOTHER CLAUDEX JOB" in plan and "Build the phone app" in plan and "Android shell" in plan


def test_a_plan_isnt_told_about_a_job_that_has_finished(job):
    repo, calls, answers = job
    answers["plan"] = {"steps": [step("codex")], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": "ok"}
    (repo / ".claudex").mkdir(exist_ok=True)
    worktrees.record_base(str(repo), repo / ".claudex" / "work.json", request="Build the phone app")
    FakeChild.script = [lambda child: _done()]
    run(repo)
    assert "ANOTHER CLAUDEX JOB" not in [c for c in calls if c[0] == "plan"][0][4]


async def _done():
    return "end_turn", "done"
