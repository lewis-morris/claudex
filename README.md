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
| `.claudex/project/journal.md` | one entry per job from every session: who did what, verdicts, files |
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

## The full screen

Typing `claudex` opens a full-screen app (like Claude and Codex). `claudex --plain` gives the simple line-by-line chat; scripts (`-p`) and Agents of Empires are unchanged.

- **Two cheeky monkeys** act out the job: Claude (coral bandana) and Codex (teal headphones) scratch their heads while the lead plans, the one working taps at a little laptop while the other watches, a banana is tossed across when work goes to the other to be checked, a thumbs-up or "!" for the review, a jump and confetti when the job's done, a droop if it stops. In between they wink, stick their tongues out and look at each other.
- **Panes:** Conversation (who said what, coloured per agent) and Plan (each step ticks off as it's done), a box to type in and a status bar.
- **How much monkey:** F2 steps through nine looks: playful, calm or off, each big, small (half-height monkeys with their names beside them) or tiny (one line of little faces like `(•‿•) Codex working…`). Calm keeps the monkeys moving gently but drops the banana, confetti and cheeky faces. `/anim calm small` jumps straight to one.
- **Keys:** F2 monkeys · F3 layout (monkeys on top / at the side / just the chat) · F4 colours (Jungle, Sunset, Daylight) · PgUp/PgDn scroll · Ctrl-C stop the job · Ctrl-D or /quit leave. Choices are remembered in `~/.config/claudex/ui.json`.
- When a worker asks permission, the options appear in the chat; type the number.
- `claudex demo` still plays the earlier pretend-job preview.

## One shared memory

Every role sees the same memory: the lead, the second opinion, both workers, both reviewers and the final check. It holds the project's `CLAUDE.md` **and** `AGENTS.md` (Claude normally reads only the first and Codex only the second), the project map, past decisions, recent jobs from every session (`.claudex/project/journal.md`) and this session's notes, including each worker's handover. A test (`test_everyone_shares_one_memory`) proves it across two sessions.

## The `/` menu and skills

- **Terminal:** type `/` and a menu pops up with every command and every skill (Claude's from `~/.claude/skills` marked [C], Codex's from `~/.codex/skills` marked [X]). Keep typing to narrow it; Tab or the arrow keys pick. `/help` (or `?`) prints the whole list. Settings commands offer their choices too (`/checks ` → full, reviews, off). Up arrow brings back earlier messages, even from past sessions. The bar at the bottom shows the current settings.
- **Skills:** `/deployify the portal` becomes "Use the deployify skill. the portal". A skill only one agent has goes to that agent; one both have is left to the lead.
- **Commands:** `/auto /claude /codex`, `/effort …`, `/mode …`, `/checks …`, `/dry`, `/status`, `/rules`, `/doctor`, `/log`, `/memory`, `/help`, `/quit`. Start a message with `@claude` or `@codex` to send just that one to one agent. Ctrl-C stops the current job but keeps the chat open.
- **Agents of Empires:** pick **claudex** as the agent. The same `/` menu appears in its message box (skills, `/rules`, `/doctor`, `/log`, `/memory`, `/status`, `/help`); settings are the Routing, Effort, Mode and Checks pickers.

## Changing who does what

Everything is in `rules.toml` (re-read on every message): the lead, reviewers, the rules table, the default, the step-up ladder, timeouts and on/off switches. After editing, run `claudex doctor` to check every model name still exists.

## Files

- `claudex/engine.py`: plan, second opinion, steps, reviews, escalation, final check, memory.
- `claudex/headless.py`: read-only one-shot calls (lead, reviews, map) with no add-ons, hooks or Chrome.
- `claudex/child.py`: drives one worker over ACP. `claudex/gitsnap.py`: before/after snapshots without touching your git index.
- `claudex/ladder.py`, `claudex/locks.py`, `claudex/tools.py` (doctor, rules, log, update).
- `claudex/server.py` (ACP agent), `claudex/cli.py` (terminal), `tests/` (`uv run --no-project --with pytest python -m pytest tests`).
- `claudex/tui.py` (the full screen and the monkeys), `claudex/commands.py` (commands and skills for both menus), `claudex/fun.py` (the chatter).
- Design and the Codex review it went through: `docs/claudex-design.md`.

## Not done yet

- Parts running at the same time (one at a time today).
- Reviewing browser-only work (no file changes means no review).
- Per-file locks between workers.
