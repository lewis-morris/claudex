"""One-shot, read-only calls to Claude or Codex that return JSON matching a schema.

Used for the lead's plan, second opinions, step reviews, the final check and the project map.
Both are isolated from your add-ons: no MCP servers, no hooks, no Chrome, read-only tools only.
"""
import asyncio
import json
import os
import tempfile

import re
import time

from . import usage as usage_log
from .limits import error_is_limit
from .paths import tool_env


class HeadlessError(Exception):
    """A one-shot call failed. `limit` is True when the failure was the agent running out of usage."""

    def __init__(self, message, limit=None):
        super().__init__(message)
        self.limit = error_is_limit(message) if limit is None else limit


def _env():
    return tool_env()


async def _run(cmd, cwd, stdin_text, timeout):
    p = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=_env(), stdin=asyncio.subprocess.PIPE,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                             start_new_session=True)
    try:
        out, err = await asyncio.wait_for(p.communicate(stdin_text.encode()), timeout)
    except BaseException:  # timeout or cancel: never leave the child running
        try:
            os.killpg(p.pid, 9)
        except Exception:
            pass
        await p.wait()
        raise
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _first_json(text):
    text = text.strip()
    obj, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    return obj


# A call with no tools (the gatekeeper, `doctor --live`) only reads the prompt and answers in JSON, so it doesn't
# need Claude Code's own long system prompt, which is most of what such a call costs.
LEAN_SYSTEM = ("You answer the user's message directly, in the JSON shape you are given. Be brief and exact. "
               "You have no tools; judge only from the message.")


def claude_cmd(model, effort, schema, tools="", budget=3.0):
    return ["claude", "-p", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "project", "--settings", '{"disableAllHooks": true}',
            "--disable-slash-commands", "--no-session-persistence", "--permission-mode", "dontAsk",
            "--tools", tools, *(["--allowedTools", tools] if tools else ["--system-prompt", LEAN_SYSTEM]),
            "--model", model, "--effort", effort, "--max-budget-usd", str(budget),
            "--output-format", "json", "--json-schema", json.dumps(schema)]


def codex_cmd(model, effort, schema_path, out_path, cwd):
    return ["codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "read-only", "-m", model, "-c", f'model_reasoning_effort="{effort}"',
            "--output-schema", schema_path, "-o", out_path, "-C", cwd, "-"]


async def claude_json(cwd, prompt, schema, model, effort, timeout=900, read_files=True, budget=3.0, role=None):
    tools = "Read,Grep,Glob" if read_files else ""
    started = time.time()
    code, out, err = await _run(claude_cmd(model, effort, schema, tools, budget), cwd, prompt, timeout)
    try:
        data = json.loads(out)
    except Exception:
        raise HeadlessError(f"claude exited {code}: {(err or out)[-600:]}")
    u = data.get("usage") or {}
    usage_log.record("claude", model, role or "one-shot", cwd,
                     fresh=(u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                     cached=u.get("cache_read_input_tokens") or 0, output=u.get("output_tokens") or 0,
                     cost=data.get("total_cost_usd"), secs=round(time.time() - started, 1))
    if code != 0 or data.get("is_error") or "structured_output" not in data:
        # keep every bit of explanation: errors[] often holds the real reason (e.g. a usage limit)
        parts = [str(data.get("result") or ""), str(data.get("subtype") or "")]
        parts += [e if isinstance(e, str) else json.dumps(e) for e in (data.get("errors") or [])]
        detail = " | ".join(x for x in parts if x) or (err or out)[-600:]
        raise HeadlessError(f"claude: {detail[:800]}")
    return data["structured_output"]


async def codex_json(cwd, prompt, schema, model, effort, timeout=900, role=None):
    with tempfile.TemporaryDirectory(prefix="claudex-") as d:
        sp, op = os.path.join(d, "schema.json"), os.path.join(d, "out.json")
        with open(sp, "w") as f:
            json.dump(schema, f)
        started = time.time()
        code, out, err = await _run(codex_cmd(model, effort, sp, op, cwd), cwd, prompt, timeout)
        used = re.findall(r"tokens used\s*\n?\s*([\d,]+)", out + "\n" + err)
        if used:
            usage_log.record("codex", model, role or "one-shot", cwd, total=int(used[-1].replace(",", "")),
                             secs=round(time.time() - started, 1))
        if code != 0:   # never trust an answer from a run that failed
            raise HeadlessError(f"codex exited {code}: {(err or out)[-800:]}")
        try:
            with open(op) as f:
                return _first_json(f.read())
        except Exception:
            raise HeadlessError(f"codex gave no answer: {(err or out)[-800:]}")


async def ask(agent, cwd, prompt, schema, model, effort, timeout=900, read_files=True, role=None):
    schema = strict(schema)
    if agent == "codex":
        return await codex_json(cwd, prompt, schema, model, effort, timeout, role=role)
    return await claude_json(cwd, prompt, schema, model, effort, timeout, read_files, role=role)


def strict(schema):
    """Codex structured output needs every object closed and every property required."""
    if isinstance(schema, dict):
        s = {k: strict(v) for k, v in schema.items()}
        if s.get("type") == "object" and "properties" in s:
            s["additionalProperties"] = False
            s["required"] = list(s["properties"])
        return s
    if isinstance(schema, list):
        return [strict(x) for x in schema]
    return schema
