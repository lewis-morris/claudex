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
    """Tests never write to the real usage history."""
    from claudex import usage
    monkeypatch.setattr(usage, "LEDGER", tmp_path / "usage.jsonl")


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

    async def fake_ask(agent, cwd, prompt, schema, model, effort, timeout=900, read_files=True, role=None):
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
    FakeChild.script, FakeChild.started = [], []
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
    assert "floats round badly" in [c for c in calls if c[0] == "plan"][1][4]
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

    must_see = ["whole pence", "pytest -q", "first job", "Earlier job"]       # both rule files + session A's work
    prompts = {kind: [c[4] for c in calls if c[0] == kind] for kind in ("plan", "opinion", "review", "final")}
    briefs = [w.prompts[0] for w in FakeChild.started]
    for who, texts in [("lead", prompts["plan"]), ("second opinion", prompts["opinion"]),
                       ("reviewers", prompts["review"]), ("final check", prompts["final"]), ("workers", briefs)]:
        assert texts, who
        for t in texts:
            for needle in must_see:
                assert needle in t, f"{who} can't see {needle!r}"
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
        t.app.layout.container.content.get_children()  # builds without error


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


def test_map_writer_gets_the_shared_memory(job):
    repo, calls, answers = job
    (repo / "CLAUDE.md").write_text("Rule for everyone.\n")
    answers["plan"] = {"steps": [step()], "big_decisions": [], "concern_responses": []}
    answers["review"] = {"verdict": "pass", "issues": [], "summary": ""}
    FakeChild.script = [("a.py", "v\n", "HANDOVER-XYZ")]
    run(repo)
    m = [c[4] for c in calls if c[0] == "map"][0]
    assert "Rule for everyone." in m and "HANDOVER-XYZ" in m


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
    assert "Routing" in t.blocks[-1]["text"]


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
    for part in ("**Done:**", "**Changed:**", "**Checked:**", "**Not checked:**", "**Needs you:**"):
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
    e, ui, stop = run(repo)
    w = FakeChild.started[0]
    assert e.ok and stop == "end_turn" and "Stopped" not in ui.all()
    assert "Claudex said no to" in w.prompts[1] and "not needed for this step" in w.prompts[1]


def test_read_only_claude_calls_are_not_in_plan_mode():
    cmd = headless.claude_cmd("sonnet", "low", {}, "Read,Grep,Glob")
    assert "plan" not in cmd and cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert "--allowedTools" not in headless.claude_cmd("sonnet", "low", {}, "")
