import asyncio
import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from composer import engine, gitsnap, headless  # noqa: E402
from composer.__main__ import chat_parser, settings_from  # noqa: E402
from composer.ladder import next_rung  # noqa: E402
from composer.locks import Lock  # noqa: E402

LADDER = engine.load_rules()["ladder"]


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


def test_snapshot_outside_git(tmp_path):
    assert gitsnap.snapshot(str(tmp_path)) is None


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
        FakeChild.started.append(self)

    async def start(self):
        pass

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

    async def fake_ask(agent, cwd, prompt, schema, model, effort, timeout=900, read_files=True):
        kind = ("plan" if schema is engine.PLAN_SCHEMA else "opinion" if schema is engine.OPINION_SCHEMA
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


def test_outside_git_refuses_unless_checks_off(job, tmp_path_factory):
    plain = tmp_path_factory.mktemp("plain")   # not inside the test repo
    e, ui, stop = run(plain)
    assert not e.ok and "isn't a git project" in ui.all() and FakeChild.started == []


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

    def short_rules(path=None):
        r = real(path)
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
    from composer.child import Child, _group_alive
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
    from composer.commands import expand_skill
    skills = {"deployify": {"description": "ship it", "agents": ["claude"]},
              "both": {"description": "", "agents": ["claude", "codex"]}}
    assert expand_skill("/deployify the portal", skills) == ("Use the deployify skill. the portal", "claude")
    assert expand_skill("/both", skills) == ("Use the both skill.", None)
    assert expand_skill("/rules", skills) is None


def test_real_skills_load_and_acp_menu_lists_them():
    from composer.commands import acp_commands, load_skills
    skills = load_skills()
    assert "deployify" in skills and not any(n.endswith("-workspace") for n in skills)
    names = [c["name"] for c in acp_commands(skills)]
    assert "rules" in names and "deployify" in names and "effort" not in names  # pickers cover settings in AoE


def test_skill_name_must_end_at_a_space():
    from composer.commands import expand_skill
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
    from composer.server import Server
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
    from composer import server
    from composer.child import _group_alive
    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "claudex"
    script.write_text("#!/bin/sh\nsleep 300 &\nsleep 300\n")
    script.chmod(0o755)
    real_path = pathlib.Path
    monkeypatch.setattr(server.pathlib, "Path", lambda *a: tmp_path / "composer" / "server.py"
                        if a and str(a[0]).endswith("server.py") else real_path(*a))
    (tmp_path / "composer").mkdir()
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
    from composer import tui as T
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
        for animate in (True, False):
            t.prefs["animate"] = animate
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
    from composer import tools
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
