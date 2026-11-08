"""A separate copy of the project for a job that would otherwise wait for another Claudex job.

Two Claudex jobs editing one folder would tangle: each step's check would see the other job's edits as its
own, and a worker committing would sweep up the other's half-done work. So when one job is already changing
files, the next one works in a git worktree (its own folder, on its own branch, made from the last commit)
and brings its changes back into the main folder when it's done:

- commits the worker made there are merged in (they stay commits, as they would have in place);
- anything left uncommitted is applied as uncommitted changes, so it looks the same as a job done in place.

If the changes clash with what's in the main folder now, nothing in the main folder is touched: the copy and
its branch are kept, and the clashing files are named.

A copy has no ignored files (installed packages, .env files), so those are linked in from the main folder,
apart from caches and Claudex's own folder. The links are added to the project's local git exclude
(`.git/info/exclude`, never a tracked file) so they never show up as changes.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

HOME = pathlib.Path.home() / ".cache" / "claudex" / "worktrees"
# Ignored things never linked into a copy: Claudex's own folder, caches, logs.
DONT_LINK = {".claudex", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".playwright-mcp",
             ".DS_Store", ".cache", ".parcel-cache", ".turbo", ".next"}
DONT_LINK_SUFFIXES = (".pyc", ".log")
MARK = "# claudex: links in its separate copies"
# Installed packages are linked even in a project that forgot to ignore them.
PACKAGES = {"node_modules", ".venv", "venv"}


class WorktreeError(Exception):
    pass


# Never part of a recorded folder state: installed packages and Claudex's own folder.
SKIP_SPECS = [":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/.venv/**", ":(exclude,glob)**/venv/**",
              ":(exclude,glob)**/__pycache__/**", ":(exclude,glob).claudex/**"]


def _git(cwd, *args, timeout=120, stdin=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, input=stdin,
                          encoding="utf-8", errors="surrogateescape")


def _ok(r, what):
    if r.returncode != 0:
        raise WorktreeError(f"{what}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def can_use(project_root):
    """A copy needs a git project with at least one commit, run from its top folder."""
    try:
        top = _git(project_root, "rev-parse", "--show-toplevel").stdout.strip()
        head = _git(project_root, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()
        return bool(top and head) and pathlib.Path(top).resolve() == pathlib.Path(project_root).resolve()
    except Exception:
        return False


def _ignored(project_root):
    """Top-level ignored files and folders (as git collapses them), plus installed packages left unignored,
    relative to the project."""
    out = _ok(_git(project_root, "status", "--ignored", "--porcelain=v1", "-z", "--untracked-files=normal"),
              "git status failed")
    found = []
    for entry in out.split("\0"):
        if entry.startswith("!! ") or (entry.startswith("?? ")
                                       and pathlib.PurePosixPath(entry[3:].rstrip("/")).name in PACKAGES):
            found.append(entry[3:].rstrip("/"))
    return found


def _linkable(rel, dont_link):
    parts = pathlib.PurePosixPath(rel).parts
    if any(p in DONT_LINK or p in dont_link for p in parts):
        return False
    return not rel.endswith(DONT_LINK_SUFFIXES)


def _exclude_links(project_root, rels):
    """Add the linked paths to the project's local exclude (shared by every worktree): as exact paths, so a
    link counts as ignored like the folder it points at (a `node_modules/` rule only matches folders)."""
    rel = _git(project_root, "rev-parse", "--git-common-dir").stdout.strip()
    if not rel:
        return
    ex = (pathlib.Path(project_root) / rel / "info" / "exclude").resolve()
    text = ex.read_text(errors="replace") if ex.exists() else ""
    have = set(text.splitlines())
    new = [f"/{r}" for r in rels if f"/{r}" not in have]
    if not new:
        return
    if MARK not in have:
        new.insert(0, MARK)
    ex.parent.mkdir(parents=True, exist_ok=True)
    ex.write_text(text + ("" if text.endswith("\n") or not text else "\n") + "\n".join(new) + "\n")


def _snapshot(project_root):
    """(head, tree): the folder as it is now, earlier finished work that nobody committed included, saved as a
    tree in the project's own git objects (nothing else in its git changes)."""
    head = _ok(_git(project_root, "rev-parse", "HEAD"), "no commit").strip()
    fd, tmp = tempfile.mkstemp(prefix="claudex-base-")
    os.close(fd)
    os.unlink(tmp)
    env = dict(os.environ, GIT_INDEX_FILE=tmp)
    try:
        for args in (("read-tree", "HEAD"), ("add", "-A", "--", ".", *SKIP_SPECS)):
            r = subprocess.run(["git", *args], cwd=project_root, env=env, capture_output=True, text=True, timeout=300)
            # git says so (and exits 1) when a skipped folder is ignored anyway; everything else was still added
            if r.returncode != 0 and not ("are ignored by one of your .gitignore" in r.stderr
                                          and "fatal:" not in r.stderr):
                _ok(r, "couldn't record the folder")
        tree = _ok(subprocess.run(["git", "write-tree"], cwd=project_root, env=env, capture_output=True, text=True,
                                  timeout=120), "couldn't record the folder").strip()
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return head, tree


def record_base(project_root, path, **about):
    """The main folder as it is now, when a job starts changing it, so a copy made while this job works starts
    from here, not from the last commit. `about` (what the job was asked, its steps) lets other jobs see what's
    being worked on."""
    head, tree = _snapshot(project_root)
    pathlib.Path(path).write_text(json.dumps({"head": head, "tree": tree, **about}))
    return tree


def folder_for(project_root, job_id):
    root = pathlib.Path(project_root).resolve()
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", root.name)[:40] + "-" + hashlib.sha1(str(root).encode()).hexdigest()[:8]
    return HOME / slug / job_id


def create(project_root, job_id, dont_link=(), base_file=None):
    """Make the copy. Returns {"path", "branch", "base", "tree", "from_now"}; raises WorktreeError if git
    can't. It starts from the folder as the other job found it (`base_file`, written by record_base), with that
    earlier work uncommitted as it is there; without one, from the folder as it is now (part-way through the
    other job's changes, which then count as already there)."""
    recorded = None
    try:
        recorded = json.loads(pathlib.Path(base_file).read_text()) if base_file else None
    except Exception:
        pass
    if not (recorded or {}).get("tree"):
        # The job with the folder didn't record how it found it (an older Claudex, or it couldn't): start from
        # the folder as it is now, so work nobody committed is in the copy rather than clashing on the way back.
        recorded = None
        head, tree = _snapshot(project_root)
    else:
        head, tree = recorded["head"], recorded["tree"]
    base = head
    head_tree = _ok(_git(project_root, "rev-parse", f"{base}^{{tree}}"), "couldn't read the last commit").strip()
    path = folder_for(project_root, job_id)
    branch = f"claudex/{job_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(project_root, "worktree", "prune")
    _ok(_git(project_root, "worktree", "add", "-q", "-b", branch, str(path), base), "couldn't make a copy")
    linked = []
    try:
        if tree != head_tree:     # bring in the earlier uncommitted work, left uncommitted
            _ok(_git(path, "read-tree", "-m", "-u", "HEAD", tree, timeout=300), "couldn't add the earlier work")
            _ok(_git(path, "reset", "-q"), "couldn't add the earlier work")
        for rel in _ignored(project_root):
            if not _linkable(rel, set(dont_link)):
                continue
            src, dst = pathlib.Path(project_root) / rel, path / rel
            if dst.exists() or dst.is_symlink():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(src.resolve(), dst)
            linked.append(rel)
        _exclude_links(project_root, linked)
    except Exception as e:
        remove(project_root, {"path": str(path), "branch": branch}, keep_branch=False)
        raise WorktreeError(f"couldn't set up the copy: {e}")
    return {"path": str(path), "branch": branch, "base": base, "tree": tree, "from_now": recorded is None,
            "linked": linked}


def _clashes(stderr):
    files = []
    for line in stderr.splitlines():
        m = (re.match(r"error: patch failed: (.+?):\d+$", line)
             or re.match(r"error: (.+?): (?:already exists in working directory|does not exist in index|patch does not apply)", line))
        if m and m.group(1) not in files:
            files.append(m.group(1))
    return files


def bring_back(project_root, wt):
    """Bring the copy's changes into the main folder. Returns (brought, clashes, message).

    brought: list of changed files now in the main folder. clashes: files that couldn't be brought back
    (nothing in the main folder was changed then)."""
    path = wt["path"]
    _ok(_git(path, "add", "-A"), "couldn't list the copy's changes")
    head_tree = _ok(_git(path, "rev-parse", f"{wt['base']}^{{tree}}"), "couldn't read the copy's start").strip()
    if wt.get("tree") and wt["tree"] != head_tree:
        # It started from earlier uncommitted work: bring back only what changed since, uncommitted (a commit
        # made in the copy would hold that earlier work too, so it can't be merged as it is).
        patch = _ok(_git(path, "diff", "--cached", "--binary", wt["tree"]), "couldn't read the copy's changes")
        files = [l for l in _ok(_git(path, "diff", "--cached", "--name-only", wt["tree"]), "diff failed").splitlines() if l]
        if not files:
            return [], [], "nothing to bring back"
        check = _git(project_root, "apply", "--check", "--binary", "-", stdin=patch)
        if check.returncode == 0:
            check = _git(project_root, "apply", "--binary", "-", stdin=patch)
        if check.returncode != 0:
            return [], _clashes(check.stderr) or files, check.stderr.strip()[:500]
        return files, [], "brought back as uncommitted changes"
    patch = _ok(_git(path, "diff", "--cached", "--binary", "HEAD"), "couldn't read the copy's changes")
    tip = _ok(_git(path, "rev-parse", "HEAD"), "couldn't read the copy's branch").strip()
    commits = _ok(_git(path, "rev-list", "--count", f"{wt['base']}..{tip}"), "couldn't count commits").strip()
    commits = int(commits or 0)
    files = [l for l in _ok(_git(path, "diff", "--name-only", wt["base"], "--cached"), "diff failed").splitlines() if l]
    if not files:
        return [], [], "nothing to bring back"
    if commits:     # the worker's own commits stay commits; git refuses (and we undo) if they'd clash
        m = _git(project_root, "merge", "--no-edit", "-q", tip, timeout=300)
        if m.returncode != 0:
            _git(project_root, "merge", "--abort")
            said = (m.stderr or m.stdout).strip()
            clash = [l.strip() for l in said.splitlines() if l.startswith("\t")] or files
            return [], clash, said[:500]
    if patch:       # the rest, as uncommitted changes; checked first so a clash leaves the main folder alone
        check = _git(project_root, "apply", "--check", "--binary", "-", stdin=patch)
        if check.returncode == 0:
            check = _git(project_root, "apply", "--binary", "-", stdin=patch)
        if check.returncode != 0:
            return ([] if not commits else files), _clashes(check.stderr) or files, check.stderr.strip()[:500]
    return files, [], f"{commits} commit(s) merged" if commits else "brought back as uncommitted changes"


def remove(project_root, wt, keep_branch=False):
    path = pathlib.Path(wt["path"])
    r = _git(project_root, "worktree", "remove", "--force", str(path))
    if r.returncode != 0 and path.exists():
        shutil.rmtree(path, ignore_errors=True)
        _git(project_root, "worktree", "prune")
    if not keep_branch and wt.get("branch"):
        _git(project_root, "branch", "-D", wt["branch"])
