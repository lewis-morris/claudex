# Claudex

One lead plans the job. Claude Code and Codex each do the parts they're best at, one at a time, check each other's work, and share one project memory. Works in the terminal and as an agent in Agents of Empires. (`composer` is the old name and still works.)

## How a job runs

1. **Lead plans** (Claude Opus 5.5, high). It reads the project files, the project map, past decisions and this session's notes. It splits the job into the fewest parts and picks agent, model and reasoning for each from `rules.toml`. Where parts connect, it writes the exact contract into both.
2. **Second opinion on big decisions.** Hard-to-undo choices (database schema, new dependency, sign-in, money/VAT logic, contracts between parts, deleting data, architecture) go to the other side (Codex GPT-6 Astra, high) before work starts. If it has concerns, the lead re-plans once and answers each one. Both views go in `decisions.md`.
3. **Workers, one at a time,** over ACP. Each keeps its own conversation for the session. A worker that hits an unsettled big decision stops and asks you (`DECISION NEEDED:`).
4. **Review after every step that changed files,** by the opposite side (Codex work → Claude Opus high; Claude work → Codex Astra high), read-only. On "fix", the issues go back to the same worker; on the third try it steps up one rung of the ladder; after that Claudex stops and tells you what's still wrong.
5. **Final check** by the lead when a job had more than one part, then the **project map** is refreshed (Codex Sol, low).

## Browser

- Only steps marked "browser" use Claude in Chrome (your signed-in Chrome), via a separate Claude worker.
- **Chrome key:** only one such step runs at a time, across every Claudex session on this PC.
- Codex can't use your signed-in Chrome; browser steps are always moved to Claude.

## Memory (in each project, hidden from git)

| File | What |
|---|---|
| `.claudex/project/map.md` | how the project is laid out (shared by all sessions) |
| `.claudex/project/decisions.md` | big decisions, second opinions, the lead's answers |
| `.claudex/sessions/<id>/notes.md` | each request, each step's handoff, each review |
| `.claudex/sessions/<id>/steps.jsonl` | who did what, model, reasoning, time, review verdict |

Only one Claudex job works in a project folder at a time; a second one waits.

## Commands

```
claudex                      chat in this folder
claudex -p "message"         one message, then exit (exit code 1 if it failed or a check found problems)
claudex --acp                agent for Agents of Empires
  --route auto|claude|codex           who does the work (default auto)
  --mode default|acceptEdits|auto|bypassPermissions   what workers may do without asking (default auto)
  --effort auto|low|medium|high|xhigh force one reasoning level for every step
  --checks full|reviews|off           second opinions + reviews / reviews only / none
  --no-review  --no-second-opinion    shortcuts for --checks
  --lead-model M  --lead-effort E     change the lead for this run
  --rules FILE                        use a different rules file
  --dry-run                           show the plan and who'd do what; change nothing
  --cwd DIR                           project folder

claudex doctor [--live]      logins, versions, connectors, every model in the rules (--live: ping each model)
claudex rules [--edit]       who does what (or open rules.toml)
claudex log [-n N]           recent steps in this project
claudex update               update Claude, Codex and both connectors, then run doctor
```

## The `/` menu and skills

- **Terminal:** type `/` and a menu pops up with every command and every skill (Claude's from `~/.claude/skills` marked [C], Codex's from `~/.codex/skills` marked [X]). Keep typing to narrow it; Tab or the arrow keys pick. `/help` (or `?`) prints the whole list. Settings commands offer their choices too (`/checks ` → full, reviews, off). Up arrow brings back earlier messages, even from past sessions. The bar at the bottom shows the current settings.
- **Skills:** `/deployify the portal` becomes "Use the deployify skill. the portal". A skill only one agent has goes to that agent; one both have is left to the lead.
- **Commands:** `/auto /claude /codex`, `/effort …`, `/mode …`, `/checks …`, `/dry`, `/status`, `/rules`, `/doctor`, `/log`, `/memory`, `/help`, `/quit`. Start a message with `@claude` or `@codex` to send just that one to one agent. Ctrl-C stops the current job but keeps the chat open.
- **Agents of Empires:** pick **claudex** as the agent. The same `/` menu appears in its message box (skills, `/rules`, `/doctor`, `/log`, `/memory`, `/status`, `/help`); settings are the Routing, Effort, Mode and Checks pickers.

## Changing who does what

Everything is in `rules.toml` (re-read on every message): the lead, reviewers, the rules table, the default, the step-up ladder, timeouts and on/off switches. After editing, run `claudex doctor` to check every model name still exists.

## Files

- `composer/engine.py`: plan, second opinion, steps, reviews, escalation, final check, memory.
- `composer/headless.py`: read-only one-shot calls (lead, reviews, map) with no add-ons, hooks or Chrome.
- `composer/child.py`: drives one worker over ACP. `composer/gitsnap.py`: before/after snapshots without touching your git index.
- `composer/ladder.py`, `composer/locks.py`, `composer/tools.py` (doctor, rules, log, update).
- `composer/server.py` (ACP agent), `composer/cli.py` (terminal), `tests/` (`uv run --no-project --with pytest python -m pytest tests`).
- `composer/commands.py` (commands and skills for both menus), `composer/fun.py` (the banner and the chatter).
- Design and the Codex review it went through: `docs/claudex-design.md`.

## Not done yet

- Parts running at the same time (one at a time today).
- Reviewing browser-only work (no file changes means no review).
- Per-file locks between workers.
