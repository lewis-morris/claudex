"""Recognising "you've run out of usage" from Claude, Codex and their APIs.

Two strengths, because the cost of a mistake differs:
- An *error* from the tool or API (an exception, a failed process) is trusted with a broad match: 429s,
  rate limits, quota and credit messages all mean this agent can't carry on right now.
- An agent's *reply text* is only trusted with narrow, provider-style notices, and only near its end
  (where a tool prints the notice when it stops). A worker that explains HTTP 429, or writes rate-limiting
  code, must not be mistaken for one that's out of usage.
"""
import re

ERROR_RE = re.compile(
    r"usage limit|rate[ _-]?limit(?:ed|_error)?|limit (?:has been |was )?reached|hit your (?:[\w-]+ )*limit|"
    r"too many requests|\b429\b|quota (?:exceeded|reached)|out of (?:extra )?(?:credits|usage)|"
    r"reached your (?:[\w-]+ )*(?:limit|quota)|credit balance is too low|usage_limit_reached|insufficient_quota",
    re.I)

NOTICE_RE = re.compile(
    r"you'?ve hit your (?:[\w-]+ )*limit|you'?ve reached your (?:[\w-]+ )*(?:limit|quota)|"
    r"(?:claude(?: ai)? )?usage limit (?:has been |was )?reached|you'?re out of (?:extra )?usage|"
    r"credit balance is too low|usage_limit_reached|rate_limit_error|insufficient_quota|"
    r"rate limit reached[^\n]{0,40}(?:try again|resets?)",
    re.I)

RESET_RE = re.compile(
    r"(?:resets?|try again|available again|back)(?:\s+(?:at|in|after|on))?\s+"
    r"((?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)|"
    r"tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)[^\n.;,]{0,30})",
    re.I)

NOTICE_WINDOW = 400   # a stop notice sits at the end of what the agent said


def error_is_limit(text):
    t = text or ""
    return bool(ERROR_RE.search(t)) or '"errorKind":"rate_limit"' in t.replace(" ", "")


_LEAD_IN = re.compile(r"^[^\w`'\"]*(?:(?:api )?error:?\s*|warning:?\s*)?", re.I)   # ⚠ ✗ ⎿ "Error:" …


def reply_is_limit(text):
    """True only for a stand-alone notice: one of the last few lines *starts* with a provider's limit message
    (after any symbols like ⚠ ✗ ⎿ or "Error:"). A sentence that merely quotes one doesn't count."""
    lines = [l.strip() for l in (text or "")[-NOTICE_WINDOW:].splitlines() if l.strip()][-3:]
    for line in lines:
        rest = line[_LEAD_IN.match(line).end():]
        if "`" not in line and NOTICE_RE.match(rest):
            return True
    return False


def reset_hint(text):
    m = RESET_RE.search(text or "")
    return m.group(1).strip() if m else None
