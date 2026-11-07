# Claudex design (Composer → Claudex)

Claudex extends the existing Composer (lead plans → one worker at a time over ACP → shared notes).
Same code base; new command name `claudex` (`composer` stays as an alias).

## 1. Lead
- `[lead]` becomes Claude Opus 5.5, effort high (architect / technical lead).
- Still a tool-less `claude -p` planner with a JSON schema, now also given the project map and
  decisions log (below).
- Plan schema gains `big_decisions: [{decision, why, alternatives}]` (may be empty). The lead is told to
  flag only hard-to-undo choices: database schema, new dependency, auth/permissions, money/VAT logic,
  public API or contract between parts, deleting data, architecture.

## 2. Second opinion (before any work starts)
- Only when `big_decisions` is non-empty and `[second_opinion] enabled`.
- Reviewer = the agent that did NOT make the plan (lead is Claude → Codex `gpt-6-astra` high).
- Run headless and read-only: `codex exec --sandbox read-only -m … -c model_reasoning_effort=… --output-schema S -o OUT`
  (or `claude -p --permission-mode plan --tools Read,Grep,Glob --json-schema S` when Codex is the planner).
- Output `{verdict: agree|concerns, concerns: [str], suggestion: str}`.
- On `concerns`: re-plan ONCE with the critique; the lead must answer each concern (accepted / rejected + why).
  Both views are appended to `.claudex/project/decisions.md`. No further loop.

## 3. Review after each step
- Snapshot the working tree before and after each step without touching the user's index:
  `GIT_INDEX_FILE=<tmp> git add -A && git write-tree` → tree hash (includes untracked, honours .gitignore).
  Diff = `git diff <before> <after>`. No diff → no review.
- Reviewer = opposite side of the worker ("code reviewer: Opus 5.5 / Astra, high"):
  Codex worker → `claude -p` opus high read-only; Claude worker → `codex exec` gpt-6-astra high read-only.
- Input: step instructions, the user's message, the diff (truncated to ~60k chars), worker's handoff.
- Output `{verdict: pass|fix, issues: [str], summary: str}`.
- On `fix`: send the issues back to the same worker (same ACP session, keeps its context).
  Second `fix` → escalate one rung on the ladder and retry. After `[review] max_rounds` (default 2) →
  stop, tell the user what's still wrong, continue no further steps.
- Skippable: `--no-review`, `[review] enabled=false`, `[review] skip_low_effort=true` skips low-effort steps.

## 4. Escalation ladder (rules.toml)
```
[ladder]
codex  = [["gpt-6.1-sol","low"],["gpt-6.1-sol","medium"],["gpt-6.1-sol","high"],["gpt-6-astra","high"],["gpt-6-astra","xhigh"]]
claude = [["sonnet","medium"],["sonnet","high"],["opus","high"],["opus","xhigh"]]
```
Next rung = first ladder entry strictly above the current (model, effort) position; if the current pair
is not on the ladder, the first entry whose effort rank is higher. Worker error also escalates.
Never Max/Ultra.

## 5. Memory
- Project-wide (shared by all sessions): `.claudex/project/map.md` (layout, key files, data, deploy),
  `.claudex/project/decisions.md`.
- Per session: `.claudex/sessions/<AoE session id|terminal>/notes.md` + `steps.jsonl`
  (steps.jsonl gains review verdicts, escalations, second-opinion outcomes).
- After a turn that changed files, refresh `map.md` with a cheap read-only call (Codex `gpt-6.1-sol` low),
  output via schema, written by Claudex. Optional `[memory] update_map`.
- `.claudex/` is added to `.git/info/exclude` (as `.composer/` is today). Old `.composer/` is left alone.

## 6. Chrome key
- Steps with `browser=true` take an exclusive `fcntl` lock on `~/.cache/claudex/chrome.lock` for the step
  (shared across all Claudex sessions/processes). While waiting, the UI says so.

## 7. Command line
```
claudex                      chat in this folder
claudex -p "msg"             one message, then exit
claudex --acp                ACP agent for Agents of Empires
  --cwd DIR --route auto|claude|codex --mode default|acceptEdits|auto|bypassPermissions
  --effort auto|low|medium|high|xhigh  --lead-model M --lead-effort E
  --no-review --no-second-opinion --rules FILE --dry-run (plan only)
claudex doctor [--live]      logins, versions, connectors, every model in rules.toml exists;
                             --live sends a tiny prompt to each distinct model
claudex rules [--edit]       show/edit rules.toml
claudex log [-n N]           recent steps for this project
claudex update               claude update; codex update; npm update -g the two ACP connectors; then doctor
```
Chat slash commands also gain /review on|off, /second on|off, /dry.

## 8. Not doing (yet)
- Parallel workers. Editing CLAUDE.md/AGENTS.md in user repos. Per-file locks (ai-lock).
