"""Snapshots of the working tree, so each step's changes can be reviewed without touching the user's index.

A snapshot copies the real index to a temporary file (so tracked files stay tracked, even ones that now
match .gitignore), stages everything into that copy with `git add -A`, and writes it as a tree object.
Both commands run with the same GIT_INDEX_FILE. Blobs land in .git/objects (harmless, garbage-collected).
"""
import os
import shutil
import subprocess
import tempfile

# Never counted as changes, even in a project with no .gitignore (build caches, installed packages).
JUNK = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc", ":(exclude,glob)**/node_modules/**",
        ":(exclude,glob)**/.venv/**", ":(exclude,glob)**/.pytest_cache/**"]


class SnapshotError(Exception):
    pass


def _git(cwd, *args, env=None, timeout=60):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)


def is_repo(cwd):
    try:
        return _git(cwd, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except Exception:
        return False


def snapshot(cwd):
    """A tree hash of the current working tree, or None if this isn't a git repo or git fails."""
    if not is_repo(cwd):
        return None
    real = _git(cwd, "rev-parse", "--git-path", "index").stdout.strip()
    real = os.path.join(cwd, real) if real and not os.path.isabs(real) else real
    fd, tmp = tempfile.mkstemp(prefix="claudex-index-")
    os.close(fd)
    try:
        if real and os.path.exists(real):
            shutil.copyfile(real, tmp)
        else:
            os.unlink(tmp)  # git creates a fresh index when the file is absent (an empty file is invalid)
        env = dict(os.environ, GIT_INDEX_FILE=tmp)
        # Tracked files first, with no exclusions (an edited tracked file is always a change, wherever it
        # lives), then new files, skipping caches and installed packages.
        if _git(cwd, "add", "-u", "--", ".", env=env, timeout=300).returncode != 0:
            return None
        if _git(cwd, "add", "-A", "--", ".", *JUNK, env=env, timeout=300).returncode != 0:
            return None
        out = _git(cwd, "write-tree", env=env)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def changed_files(cwd, before, after):
    if not before or not after or before == after:
        return []
    out = _git(cwd, "diff", "--name-only", before, after)
    if out.returncode != 0:
        raise SnapshotError(f"git diff failed: {out.stderr.strip()[:200]}")
    return [l for l in out.stdout.splitlines() if l]


def diff(cwd, before, after, limit=60000):
    """(diff text, list of files, truncated?) between two snapshots. Truncation is always reported."""
    files = changed_files(cwd, before, after)
    if not files:
        return "", [], False
    res = _git(cwd, "diff", "--stat", "--patch", before, after, timeout=120)
    if res.returncode != 0:
        raise SnapshotError(f"git diff failed: {res.stderr.strip()[:200]}")
    text = res.stdout
    if len(text) <= limit:
        return text, files, False
    return text[:limit], files, True
