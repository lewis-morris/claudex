# Composer

One lead decides, for each part of a job, whether **Claude Code** or **Codex** does it, and on which model and reasoning level. Only one worker runs at a time. Both read the same shared notes, so each picks up where the other left off.

## Use it

- **Terminal:** `composer` in a project folder starts a chat. `composer -p "message"` runs one message and exits.
  - `/auto`, `/claude` and `/codex` set who does the work.
  - `/effort low|medium|high|xhigh|auto` and `/mode default|acceptEdits|auto|bypassPermissions` set reasoning and permissions.
  - Start a message with `@claude` or `@codex` to send just that one message to one agent.
- **Agents of Empires:** pick the **composer** agent for a new session. Its picker options:
  - **Routing:** Auto, Claude only or Codex only.
  - **Effort:** Auto, or a fixed level for every step.
  - **Mode:** how much the workers may do without asking.

## How it works

1. **Lead:** a slimmed-down `claude -p` call (no add-ons or tools, about 1p a plan) reads the message, the project's files, the shared notes and `rules.toml`. It returns 1 to 6 steps, each with agent, model, effort, browser yes or no, and why.
2. **Workers:** each step runs on the real agent through its ACP connector:
   - Claude via `claude-agent-acp-chrome`, so it has Claude in Chrome.
   - Codex via `codex-acp`.
   - Messages, tool cards and permission questions pass straight through, and the plan shows its progress.
   - Each agent keeps its own conversation for the whole session.
3. **Shared memory:** `.composer/<AoE session id or "terminal">/notes.md` in the project folder holds every request and each step's handoff. `.composer/steps.jsonl` logs agent, model, effort, time and outcome for each step. The folder is hidden from git via `.git/info/exclude`.

## Keeping it current

- `rules.toml` is re-read on every message. Edit the rules, the default (GPT-6.1 Sol, medium) and the lead model there.
- Requested models are checked against what each agent offers right now. A renamed or retired model falls back to that agent's default instead of failing.
- Review `steps.jsonl` from time to time to see which routes work, and adjust the rules.

## Browser

- **Claude:** your signed-in Chrome, through Claude in Chrome.
- **Codex:** its own Browser add-on only works inside the Codex app, so from here it uses Playwright (a separate, signed-out browser).
- Steps needing the signed-in browser are always routed to Claude (`[browser] signed_in` in `rules.toml`).

## Files

- `composer/engine.py`: planning, routing, notes.
- `composer/child.py`: drives one worker over ACP.
- `composer/server.py`: the ACP agent for AoE.
- `composer/cli.py`: the terminal chat.
- `tools/acp_client.py`: a stand-in for AoE, for testing.
- **AoE settings:** `~/.config/agent-of-empires/config.toml` has `composer` in `[session.custom_agents]` and `[session.agent_acp_cmd]`. The backup is `config.toml.bak-20261106-191430-pre-composer`.
