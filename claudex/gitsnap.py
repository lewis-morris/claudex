"""Before-and-after snapshots of the project, so each step's changes can be seen and checked.

Claudex keeps its own private git store in .claudex/snapshots.git and points it at the project folder. That
means snapshots work the same in any folder: a git project, a brand-new one with no commits yet, an odd one,
or not a git project at all. And nothing is ever written into the project's own git data.

What counts: everything in the folder that the project's .gitignore doesn't exclude, minus build caches and
installed packages; plus every file the project's own git tracks, wherever it lives (so an edited tracked file
is always a change). The .claudex folder itself never counts.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile

STORE = ".claudex/snapshots.git"
# Never counted as changes, even in a project with no .gitignore (build caches, installed packages).
JUNK = ["__pycache__/", "*.pyc", "node_modules/", ".venv/", ".pytest_cache/", ".claudex/"]
# The same, as pathspecs: these win even over a .gitignore that re-includes them (!node_modules/).
JUNK_SPECS = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc", ":(exclude,glob)**/node_modules/**",
              ":(exclude,glob)**/.venv/**", ":(exclude,glob)**/.pytest_cache/**"]
LAST_ERROR = ""          # why the last snapshot failed, in git's words, for the message you see


class SnapshotError(Exception):
    pass


def _git(cwd, *args, env=None, timeout=60, stdin=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=stdin is None or isinstance(stdin, str),
                          env=env, timeout=timeout, input=stdin)


def is_repo(cwd):
    try:
        return _git(cwd, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except Exception:
        return False


def _store(root):
    store = pathlib.Path(root) / STORE
    if not (store / "HEAD").exists():
        store.parent.mkdir(parents=True, exist_ok=True)
        r = _git(root, "init", "--bare", "-q", str(store))
        if r.returncode != 0:
            raise SnapshotError(r.stderr.strip()[:200] or "git init failed")
        (store / "info").mkdir(exist_ok=True)
        (store / "info" / "exclude").write_text("\n".join(JUNK) + "\n")
        _hide_from_project_git(root)
    return store


def _hide_from_project_git(root):
    """Keep .claudex/ out of the project's own `git status` (via .git/info/exclude, never a tracked file)."""
    try:
        rel = _git(root, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not rel:
            return
        ex = pathlib.Path(root) / rel
        text = ex.read_text() if ex.exists() else ""
        if ".claudex/" not in text.split():
            ex.parent.mkdir(parents=True, exist_ok=True)
            ex.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".claudex/\n")
    except Exception:
        pass


def _sg(root, *args, env=None, timeout=120, stdin=None):
    """git on Claudex's private store, looking at the project folder."""
    store = _store(root)
    return _git(root, f"--git-dir={store}", f"--work-tree={root}", "-c", "core.autocrlf=false",
                "-c", "core.safecrlf=false", *args, env=env, timeout=timeout, stdin=stdin)


def _sync_excludes(root, store):
    """The store ignores what the project's own git ignores locally (.git/info/exclude), plus the junk."""
    extra = ""
    try:
        rel = _git(root, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if rel and (pathlib.Path(root) / rel).is_file():
            extra = (pathlib.Path(root) / rel).read_text(errors="replace")
    except Exception:
        pass
    want = "\n".join(JUNK) + "\n" + extra
    ex = store / "info" / "exclude"
    if not ex.exists() or ex.read_text(errors="replace") != want:
        ex.write_text(want)


def snapshot(cwd):
    """A tree hash of the folder as it is now, or None (with LAST_ERROR saying why) if git couldn't do it."""
    global LAST_ERROR
    root = str(pathlib.Path(cwd).resolve())
    fd, tmp = tempfile.mkstemp(prefix="claudex-index-")
    os.close(fd)
    os.unlink(tmp)
    try:
        store = _store(root)
        _sync_excludes(root, store)
        warm = store / "claudex-index"        # the last snapshot's index: its timestamps let git skip unchanged files
        if warm.exists():
            shutil.copyfile(warm, tmp)
    except Exception as e:
        LAST_ERROR = str(e)[:200]
        return None
    env = dict(os.environ, GIT_INDEX_FILE=tmp)
    try:
        r = _sg(root, "add", "-A", "--", ".", *JUNK_SPECS, env=env, timeout=300)
        if r.returncode != 0:
            LAST_ERROR = (r.stderr or r.stdout).strip().splitlines()[-1][:200] if (r.stderr or r.stdout).strip() else "git add failed"
            return None
        tracked = []
        if is_repo(root):             # files the project's own git tracks always count, even in ignored folders
            ls = _git(root, "ls-files", "-z", "--cached")
            tracked = [f for f in ls.stdout.split("\0") if f and os.path.lexists(os.path.join(root, f))]
            if ls.returncode != 0:
                LAST_ERROR = f"couldn't list the project's tracked files: {ls.stderr.strip()[:150]}"
                return None
            if tracked:               # names, not patterns: a file called '*.txt' or ':x' means just that file
                r2 = _git(root, "--literal-pathspecs", f"--git-dir={_store(root)}", f"--work-tree={root}",
                          "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", env=env, timeout=300,
                          stdin="\0".join(tracked))
                if r2.returncode != 0:
                    LAST_ERROR = (r2.stderr.strip().splitlines() or ["git add of tracked files failed"])[-1][:200]
                    return None
        # The warm index may remember files that are gone, or that are now ignored: drop those, so a deletion
        # shows as a deletion and a newly ignored file stops counting.
        fresh_env = dict(os.environ, GIT_INDEX_FILE=tmp + ".empty")
        eligible = _sg(root, "ls-files", "-z", "--others", "--exclude-standard", "--", ".", *JUNK_SPECS,
                       env=fresh_env, timeout=300)
        held = _sg(root, "ls-files", "-z", env=env, timeout=120)
        if eligible.returncode == 0 and held.returncode == 0:
            keep = set(f for f in eligible.stdout.split("\0") if f) | set(tracked if is_repo(root) else [])
            stale = [f for f in held.stdout.split("\0") if f and f not in keep]
            if stale:
                _git(root, "--literal-pathspecs", f"--git-dir={_store(root)}", f"--work-tree={root}", "rm",
                     "--cached", "-q", "--ignore-unmatch", "--pathspec-from-file=-", "--pathspec-file-nul",
                     env=env, timeout=300, stdin="\0".join(stale))
        out = _sg(root, "write-tree", env=env)
        if out.returncode != 0 or not out.stdout.strip():
            LAST_ERROR = out.stderr.strip()[:200] or "git write-tree failed"
            return None
        LAST_ERROR = ""
        try:
            shutil.copyfile(tmp, store / "claudex-index")
        except Exception:
            pass
        return out.stdout.strip()
    except FileNotFoundError:
        LAST_ERROR = "git isn't installed"
        return None
    except Exception as e:
        LAST_ERROR = str(e)[:200]
        return None
    finally:
        for leftover in (tmp, tmp + ".empty"):
            if os.path.exists(leftover):
                os.unlink(leftover)


def changed_files(cwd, before, after):
    if not before or not after or before == after:
        return []
    out = _sg(str(pathlib.Path(cwd).resolve()), "diff", "--name-only", before, after)
    if out.returncode != 0:
        raise SnapshotError(f"git diff failed: {out.stderr.strip()[:200]}")
    return [l for l in out.stdout.splitlines() if l]


def diff(cwd, before, after, limit=60000):
    """(diff text, list of files, truncated?) between two snapshots. Truncation is always reported."""
    files = changed_files(cwd, before, after)
    if not files:
        return "", [], False
    res = _sg(str(pathlib.Path(cwd).resolve()), "diff", "--stat", "--patch", before, after, timeout=120)
    if res.returncode != 0:
        raise SnapshotError(f"git diff failed: {res.stderr.strip()[:200]}")
    text = res.stdout
    if len(text) <= limit:
        return text, files, False
    return text[:limit], files, True
