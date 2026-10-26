"""Release wrap-up helper: archive the published evidence, then retire the release tree.

Four subcommands, all dry-run unless --apply is given. None of them rebuilds or touches a published
asset; the only remote writes are `push` (fast-forward, never forced) and the explicit
`retire --remote` deletion of a merged branch. `commit` goes through the repository's real pre-commit
hook and has no way to skip it.

  archive VERSION --tree T   copy the quality records whose SHA-256 matches the release receipt
                             (post_release.json) plus the receipt companions into
                             outputs/release-candidates/vVERSION/ and extend archive-index.json.
  retire --tree T            move a finished linked worktree to the Trash and drop its merged branch,
                             after proving nothing unpreserved would be lost.
  commit --path P... -m F    stage exactly the named files, run commit_preflight, commit through the real
                             hook, then check the commit (parent, tree, files, identity) against the plan.
  push                       fast-forward the checked-out branch to its remote, then read the remote tip
                             back from the actual push URL. Refuses anything that is not a fast-forward.

Exit codes: 0 done (or dry-run plan printed), 1 refused or failed, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import importlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
gate_stamp = importlib.import_module("tools.gate_stamp")

ARCHIVE_DIR = "outputs/release-candidates"
INDEX_NAME = "archive-index.json"
COMPANIONS = ("post_release.json", "publish.log", "online-manifest.json")
DEFAULT_BASE = "feat/target-price"


class WrapupError(RuntimeError):
    """A refusal: the message says what is missing or unsafe, nothing was changed."""


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WrapupError("cannot read %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise WrapupError("%s is not a JSON object" % path)
    return data


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), env=gate_stamp.git_env(),
                          capture_output=True, encoding="utf-8", errors="surrogateescape",
                          timeout=60, check=False)


def redact(text: str) -> str:
    """Hide credentials embedded in URLs (https://user:token@host/...) before anything is printed."""
    return re.sub(r"(?<=//)[^/@\s]+@", "***@", text)


def git_out(cwd: Path, *args: str) -> str:
    result = git(cwd, *args)
    if result.returncode != 0:
        raise WrapupError(redact("git %s failed: %s" % (" ".join(args), result.stderr.strip())))
    return result.stdout.strip()


def repo_root(start: Path) -> Path:
    return Path(os.path.realpath(git_out(start, "rev-parse", "--show-toplevel")))


def find_unique(tree: Path, version: str, digest: str) -> Path:
    """The one root-level quality-vVERSION*.json whose SHA-256 is exactly digest."""
    hits = [p for p in sorted(tree.glob("quality-v%s*.json" % version)) if sha256_file(p) == digest]
    if len(hits) != 1:
        raise WrapupError("expected exactly one quality record with sha256 %s in %s, found %d"
                          % (digest[:12], tree, len(hits)))
    return hits[0]


def find_complete(tree: Path, version: str, fingerprint: Any) -> Path:
    """The one stage=complete record bound to the same artifact fingerprint as the candidate."""
    hits = []
    for path in sorted(tree.glob("quality-v%s*.json" % version)):
        if path.name.endswith(".manifest.json"):
            continue
        data = read_json(path)
        stage = (data.get("release_qualification") or {}).get("stage")
        if stage == "complete" and data.get("artifact_fingerprint") == fingerprint:
            hits.append(path)
    if len(hits) != 1:
        raise WrapupError("expected exactly one complete-stage record bound to the candidate, found %d"
                          % len(hits))
    return hits[0]


def manifest_of(record: Path) -> Path:
    path = record.with_name(record.name[:-len(".json")] + ".manifest.json")
    if not path.is_file():
        raise WrapupError("missing manifest next to %s" % record.name)
    return path


def plan_archive(tree: Path, version: str) -> list[tuple[Path, str]]:
    """(source file, destination name inside the archive folder) for every file to keep."""
    folder = tree / ARCHIVE_DIR / ("v" + version)
    receipt = read_json(folder / "post_release.json")
    if receipt.get("version") != version:
        raise WrapupError("post_release.json is for version %r, not %s" % (receipt.get("version"), version))
    for key in ("candidate_sha256", "candidate_manifest_sha256"):
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            raise WrapupError("post_release.json carries no %s" % key)
    candidate = find_unique(tree, version, receipt["candidate_sha256"])
    manifest = find_unique(tree, version, receipt["candidate_manifest_sha256"])
    if manifest != manifest_of(candidate):
        raise WrapupError("the receipt manifest is not the one next to %s" % candidate.name)
    complete = find_complete(tree, version, read_json(candidate).get("artifact_fingerprint"))
    files = [candidate, manifest, complete, manifest_of(complete)]
    files += [folder / name for name in COMPANIONS]
    missing = [p.name for p in files if not p.is_file()]
    if missing:
        raise WrapupError("missing evidence files: " + ", ".join(missing))
    return [(p, p.name) for p in files]


def dump_index(index: dict[str, Any]) -> str:
    """Same layout as the committed file: indent 1, sorted keys, no trailing newline."""
    return json.dumps(index, indent=1, sort_keys=True, ensure_ascii=False)


def archive(repo: Path, tree: Path, version: str, apply: bool) -> dict[str, int]:
    """Copy the receipt-matched evidence into the repo archive and index it (idempotent)."""
    plan = plan_archive(tree, version)
    folder = repo / ARCHIVE_DIR / ("v" + version)
    index_path = repo / ARCHIVE_DIR / INDEX_NAME
    index = read_json(index_path)
    known = {entry["to"]: entry["sha256"] for entry in index["files"]}
    work = []
    for source, name in plan:
        digest = sha256_file(source)
        dest = folder / name
        to = "%s/v%s/%s" % (ARCHIVE_DIR, version, name)
        if dest.exists() and sha256_file(dest) != digest:
            raise WrapupError("%s already exists with different bytes; it is never overwritten" % to)
        if known.get(to, digest) != digest:
            raise WrapupError("%s is indexed with a different SHA-256; it is never rewritten" % to)
        work.append((source, dest, to, digest))
    summary = {"copy": 0, "index": 0, "keep": 0}
    added = []
    for source, dest, to, digest in work:
        need_copy = not dest.exists()
        need_index = to not in known
        summary["copy"] += need_copy
        summary["index"] += need_index
        summary["keep"] += not (need_copy or need_index)
        print("%s %s%s" % (digest[:12], to, "" if need_copy or need_index else "  (already archived)"))
        if apply and need_copy:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            if sha256_file(dest) != digest:
                raise WrapupError("copy of %s changed the bytes; stopped, nothing indexed" % to)
        if need_index:
            added.append({"archived_at": utc_now(), "from": os.path.relpath(source, tree),
                          "sha256": digest, "to": to})
    if apply and added:
        index["files"].extend(added)
        if tree != repo:
            index["notes"].append("v%s entries were copied (not moved) from the isolated release tree "
                                  "%s on archived_at; 'from' is relative to that tree's root."
                                  % (version, tree.name))
        index_path.write_text(dump_index(index), encoding="utf-8")
    print("%s: copy %d, index %d, already archived %d" % ("applied" if apply else "dry-run (no --apply)",
                                                          summary["copy"], summary["index"], summary["keep"]))
    return summary


def worktree_records(repo: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for block in git_out(repo, "worktree", "list", "--porcelain").split("\n\n"):
        record = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            record[key] = value
        if record:
            records.append(record)
    return records


def blob_sha256(repo: Path, spec: str) -> str | None:
    """SHA-256 of a committed blob's bytes, or None when the spec does not exist."""
    result = subprocess.run(["git", "cat-file", "blob", spec], cwd=str(repo), env=gate_stamp.git_env(),
                            capture_output=True, timeout=60, check=False)
    return hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None


def preserved_elsewhere(repo: Path, base: str, tree: Path, name: str) -> bool:
    """An untracked file is safe to drop if base holds the same bytes, either at the same path
    or as an entry of the committed archive index whose archived copy still matches."""
    here = git_out(tree, "hash-object", "--", name)
    same = git(repo, "rev-parse", "--verify", "-q", "%s:%s" % (base, name))
    if same.returncode == 0 and same.stdout.strip() == here:
        return True
    digest = sha256_file(tree / name)
    shown = git(repo, "show", "%s:%s/%s" % (base, ARCHIVE_DIR, INDEX_NAME))
    entries = json.loads(shown.stdout)["files"] if shown.returncode == 0 else []
    return any(e["sha256"] == digest and blob_sha256(repo, "%s:%s" % (base, e["to"])) == digest
               for e in entries)


def unsaved_work(repo: Path, base: str, tree: Path, allow: list[str]) -> list[str]:
    """Reasons the tree still holds work that exists nowhere else (empty list = safe)."""
    status = git(tree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.returncode != 0:
        raise WrapupError("git status failed in %s: %s" % (tree, status.stderr.strip()))
    raw = status.stdout  # not stripped: the first column of the first entry may be a space
    reasons = []
    for entry in [e for e in raw.split("\0") if e]:
        name = entry[3:]
        if entry[:2] != "??":
            reasons.append("uncommitted change: " + name)
        elif name not in allow and not preserved_elsewhere(repo, base, tree, name):
            reasons.append("untracked and not preserved: " + name)
    return reasons


def processes_using(tree: Path) -> list[str]:
    """Command lines of other running processes that mention the tree path."""
    result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, encoding="utf-8",
                            errors="replace", timeout=30, check=False)
    if result.returncode != 0:
        raise WrapupError("cannot list running processes, so cannot prove the tree is idle")
    mention = re.compile(re.escape(str(tree)) + r"(?![\w.-])")
    return [line.strip() for line in result.stdout.splitlines()
            if mention.search(line) and "release_wrapup" not in line
            and line.split(None, 1)[0] != str(os.getpid())]


def cwd_holders(tree: Path) -> list[str]:
    """Processes whose working directory is the tree or inside it (an idle shell counts)."""
    result = subprocess.run(["lsof", "-d", "cwd", "-Fpn"], capture_output=True, encoding="utf-8",
                            errors="replace", timeout=30, check=False)
    if not result.stdout.strip():  # lsof exits 1 for unreadable processes too; no output at all is the failure
        raise WrapupError("cannot list working directories, so cannot prove the tree is idle")
    holders, pid = [], ""
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and (line[1:] == str(tree) or line[1:].startswith(str(tree) + os.sep)):
            holders.append("pid %s has its working directory at %s" % (pid, line[1:]))
    return holders


def check_retirable(repo: Path, tree: Path, base: str, allow: list[str]) -> dict[str, str]:
    """Every refusal for retire lives here; returns the worktree record when all checks pass."""
    records = worktree_records(repo)
    match = [r for r in records if os.path.realpath(r["worktree"]) == str(tree)]
    if not match or not tree.is_dir():
        raise WrapupError("%s is not a registered, existing worktree" % tree)
    record = match[0]
    if record is records[0]:
        raise WrapupError("%s is the main worktree; it is never retired" % tree)
    if "locked" in record:
        raise WrapupError("%s is locked: %s" % (tree, record["locked"] or "no reason given"))
    if git(repo, "rev-parse", "--verify", "-q", base).returncode != 0:
        raise WrapupError("base ref %s does not exist" % base)
    if record.get("branch") == "refs/heads/" + base:
        raise WrapupError("the tree is on the base branch %s itself" % base)
    if git(repo, "merge-base", "--is-ancestor", record["HEAD"], base).returncode != 0:
        raise WrapupError("tree HEAD %s is not contained in %s: its commits would be lost"
                          % (record["HEAD"][:12], base))
    problems = unsaved_work(repo, base, tree, allow)
    problems += ["still in use: " + line for line in processes_using(tree) + cwd_holders(tree)]
    if problems:
        raise WrapupError("the tree is not safe to retire:\n  " + "\n  ".join(problems))
    return record


def remote_branch_state(repo: Path, branch: str, base: str) -> str:
    """'absent', or the SHA of origin/branch once proven contained in base."""
    listed = git_out(repo, "ls-remote", "--heads", "origin", branch)
    if not listed:
        return "absent"
    sha = listed.split()[0]
    if git(repo, "merge-base", "--is-ancestor", sha, base).returncode != 0:
        raise WrapupError("origin/%s (%s) is not contained in %s; not deleting it" % (branch, sha[:12], base))
    return sha


def trash_destination(trash: Path, tree: Path) -> Path:
    stamp = utc_now().replace(":", "").replace("-", "").split("+")[0]
    dest = trash / ("%s-%s" % (tree.name, stamp))
    count = 1
    while dest.exists():
        count += 1
        dest = trash / ("%s-%s-%d" % (tree.name, stamp, count))
    return dest


def retire(repo: Path, tree: Path, base: str, allow: list[str], trash: Path,
           remote: bool, apply: bool) -> dict[str, str]:
    """Move a finished linked worktree to the Trash, then drop its merged branch (never forced)."""
    record = check_retirable(repo, tree, base, allow)
    branch = record.get("branch", "")[len("refs/heads/"):]
    remote_state = remote_branch_state(repo, branch, base) if remote and branch else "not requested"
    dest = trash_destination(trash, tree)
    result = {"tree": "planned", "branch": branch or "detached", "remote": remote_state}
    print("move %s -> %s" % (tree, dest))
    print("branch %s: delete if merged; origin: %s" % (branch or "(detached)", remote_state))
    if not apply:
        print("dry-run (no --apply): nothing changed")
        return result
    trash.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tree), str(dest))
    git_out(repo, "worktree", "prune")
    result["tree"] = "moved"
    if branch:
        deleted = git(repo, "branch", "-d", branch)
        result["branch"] = "deleted" if deleted.returncode == 0 else "kept: " + deleted.stderr.strip()
    if remote_state not in ("absent", "not requested"):
        pushed = git(repo, "push", "origin", "--delete", branch)
        if pushed.returncode != 0:
            raise WrapupError("the tree is already in the Trash, but deleting origin/%s failed: %s"
                              % (branch, pushed.stderr.strip()))
        result["remote"] = "deleted"
    print("retired: tree %s, branch %s, origin %s" % (result["tree"], result["branch"], result["remote"]))
    return result


def check_paths(paths: list[str]) -> None:
    """Same exact-file scope rule commit_preflight enforces: distinct, relative, no '..'."""
    bad = [p for p in paths if not p or p != PurePosixPath(p).as_posix() or p.startswith("/")
           or ".." in PurePosixPath(p).parts or p == "."]
    if not paths or len(set(paths)) != len(paths) or bad:
        raise WrapupError("--path needs one or more distinct, exact, repo-relative files (bad: %s)"
                          % (bad or paths))


def parse_author(text: str) -> tuple[str, str]:
    match = re.fullmatch(r"\s*([^<>]+?)\s*<([^<>@\s]+@[^<>@\s]+)>\s*", text)
    if not match:
        raise WrapupError("--author must look like 'Name <email>', got %r" % text)
    return match.group(1), match.group(2)


def commit_env(identity: tuple[str, str] | None = None) -> dict[str, str]:
    """Environment for preflight and commit: no GIT_* routing, no automatic failure retry, and the
    identity applied to this one commit only (the repository's configured identity is never touched)."""
    env = gate_stamp.git_env()
    env["SF_RERUN_FAILED"] = "0"
    if identity:
        name, email = identity
        env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                   GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    return env


def preflight_problems(repo: Path, paths: list[str], head: str) -> str:
    """Run the repository's own commit_preflight; '' when it passes, otherwise what it found."""
    cmd = [sys.executable, str(Path(__file__).with_name("commit_preflight.py")), "--expected-head", head]
    for path in paths:
        cmd += ["--path", path]
    result = subprocess.run(cmd, cwd=str(repo), env=commit_env(), capture_output=True, encoding="utf-8",
                            errors="replace", timeout=90, check=False)
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise WrapupError("commit_preflight gave no report: " + result.stderr.strip()[-300:]) from exc
    if report.get("checks_passed"):
        return ""
    return "; ".join(json.dumps(i, ensure_ascii=False, sort_keys=True) for i in report.get("issues", [])) \
        or "preflight failed without naming a reason"


def changed_paths(repo: Path) -> set[str]:
    """Every file with a pending change (modified, deleted, new), one entry per file."""
    status = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.returncode != 0:
        raise WrapupError("git status failed: " + status.stderr.strip())
    entries, found = [e for e in status.stdout.split("\0") if e], set()
    skip = False
    for entry in entries:
        if skip:  # the old name that follows a rename or copy entry
            skip = False
            continue
        skip = entry[0] in "RC"
        found.add(entry[3:])
    return found


def head_identity(repo: Path) -> tuple[str, str]:
    name, _, email = git_out(repo, "log", "-1", "--format=%an%x00%ae").partition("\0")
    return name, email


def run_commit(repo: Path, message_file: Path, env: dict[str, str], log: Path, timeout: float) -> int:
    """`git commit -F` through the real hook. On timeout the whole process group is stopped, so a
    hook's own children cannot outlive the call. There is no way to skip the hook from here."""
    with log.open("wb") as sink:
        proc = subprocess.Popen(["git", "commit", "-F", str(message_file)], cwd=str(repo), env=env,
                                stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            with contextlib.suppress(ProcessLookupError):  # sweep whatever ignored the TERM
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise WrapupError("git commit did not finish within %ds and was stopped; the hook may have been "
                              "mid-run, so check `git log` and `git status` before trying again" % timeout)


def verify_commit(repo: Path, parent: str, tree: str, paths: list[str], identity: tuple[str, str]) -> str:
    """The commit that now exists must be exactly the one that was reviewed."""
    new = git_out(repo, "rev-parse", "HEAD")
    if new == parent:
        raise WrapupError("HEAD did not move, so no commit was made")
    if git_out(repo, "rev-parse", "HEAD^") != parent:
        raise WrapupError("commit %s does not sit directly on the head it was planned against" % new[:12])
    if git_out(repo, "rev-parse", "HEAD^{tree}") != tree:
        raise WrapupError("commit %s has a different tree from the one reviewed: a hook changed the index"
                          % new[:12])
    listed = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--no-renames", "HEAD")
    if sorted(n for n in listed.stdout.split("\0") if n) != sorted(paths):
        raise WrapupError("commit %s changes files other than the requested paths" % new[:12])
    check_identity(repo, new, identity)
    return new


def check_identity(repo: Path, new: str, identity: tuple[str, str]) -> None:
    """Author and committer of HEAD must both be the planned identity."""
    actual = git_out(repo, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce").split("\0")
    if actual != [identity[0], identity[1], identity[0], identity[1]]:
        raise WrapupError("commit %s carries the identity %s, not the planned %s <%s>"
                          % (new[:12], actual, identity[0], identity[1]))


def unstage(repo: Path, paths: list[str]) -> None:
    """Put the index back for these paths; never raises, so it cannot hide the error being reported."""
    git(repo, "reset", "-q", "--", *paths)


def read_message(message_file: Path) -> str:
    """The commit message text; an unreadable or blank file is refused."""
    try:
        message = message_file.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise WrapupError("cannot read the message file %s: %s" % (message_file, exc)) from exc
    if not message.strip():
        raise WrapupError("the message file %s is empty" % message_file)
    return message


def branch_and_head(repo: Path, expected_head: str | None) -> tuple[str, str]:
    """The checked-out branch and its tip; detached HEAD or a tip other than the expected one is refused."""
    branch = git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    head = git_out(repo, "rev-parse", "HEAD")
    if not branch:
        raise WrapupError("HEAD is detached; commit on a branch")
    if expected_head and expected_head != head:
        raise WrapupError("HEAD is %s, not the expected %s" % (head[:12], expected_head[:12]))
    return branch, head


def check_ready(repo: Path, paths: list[str], message_file: Path, author: str | None,
                expected_head: str | None) -> dict[str, Any]:
    """Every refusal `commit` can make before it touches the index, with nothing printed and nothing changed.

    A queue calls this before it takes a place in line, so a staged index or a missing change shows up at
    once instead of after the wait."""
    check_paths(paths)
    message = read_message(message_file)
    branch, head = branch_and_head(repo, expected_head)
    if git_out(repo, "diff", "--cached", "--name-only"):
        raise WrapupError("the index already holds staged changes; commit or unstage them first")
    missing = sorted(set(paths) - changed_paths(repo))
    if missing:
        raise WrapupError("no pending change in: " + ", ".join(missing))
    identity = parse_author(author) if author else head_identity(repo)
    return {"branch": branch, "head": head, "paths": paths, "author": "%s <%s>" % identity,
            "identity": identity, "message": message}


def commit(repo: Path, paths: list[str], message_file: Path, author: str | None, expected_head: str | None,
           apply: bool, timeout: float = 1800, log: Path | None = None) -> dict[str, Any]:
    """Stage exactly `paths`, run commit_preflight, commit through the real hook, check the result."""
    ready = check_ready(repo, paths, message_file, author, expected_head)
    branch, head, message, identity = ready["branch"], ready["head"], ready["message"], ready["identity"]
    result: dict[str, Any] = {key: ready[key] for key in ("branch", "head", "paths", "author")}
    print("commit on %s (HEAD %s) as %s <%s>: %s" % (branch, head[:12], *identity, ", ".join(paths)))
    print("message: " + message.strip().splitlines()[0])
    if not apply:
        print("dry-run (no --apply): nothing staged, nothing committed")
        return result
    git_out(repo, "add", "--", *paths)
    problems = preflight_problems(repo, paths, head)
    if problems:
        unstage(repo, paths)
        raise WrapupError("commit_preflight refused, nothing committed: " + problems)
    tree = git_out(repo, "write-tree")
    if log is None:
        handle, name = tempfile.mkstemp(prefix="wrapup-commit-", suffix=".log")
        os.close(handle)
        log = Path(name)
    sink = log
    print("running the pre-commit hook (a full gate takes minutes); live output: " + str(sink))
    try:
        code = run_commit(repo, message_file, commit_env(identity), sink, timeout)
    except WrapupError:
        unstage(repo, paths)
        raise
    if code != 0:
        unstage(repo, paths)
        tail = sink.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise WrapupError("git commit exited %d, nothing committed (log %s):\n%s" % (code, sink, tail))
    result.update(commit=verify_commit(repo, head, tree, paths, identity), tree=tree, log=str(sink))
    print("committed %s on %s; tree %s matches the reviewed one" % (result["commit"][:12], branch, tree[:12]))
    return result


def merge_heads(repo: Path) -> list[str]:
    """The commits an unfinished merge brings in (one per MERGE_HEAD line); empty when no merge is under way."""
    where = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").stdout.strip()
    try:
        return Path(where).read_text(encoding="utf-8").split()
    except OSError:
        return []


def merge_state(repo: Path) -> dict[str, Any]:
    """What an unfinished merge looks like right now, as plain data a caller can freeze and compare later.

    Nothing here raises: a value that cannot be read becomes one that fails the comparison."""
    head, tree = git(repo, "rev-parse", "HEAD"), git(repo, "write-tree")
    unmerged = git(repo, "diff", "--name-only", "-z", "--diff-filter=U").stdout.split("\0")
    return {"head": head.stdout.strip() if head.returncode == 0 else "",
            "merge_heads": merge_heads(repo),
            "tree": tree.stdout.strip() if tree.returncode == 0 else "",
            "unmerged": sorted(name for name in unmerged if name),
            "worktree_clean": git(repo, "diff", "--quiet", "--exit-code").returncode == 0}


def check_merge_ready(repo: Path, message_file: Path, author: str | None, expected_head: str | None,
                      source: str | None, expected_tree: str | None) -> dict[str, Any]:
    """Every refusal `merge_commit` can make before it runs the hook; nothing printed, nothing changed."""
    message = read_message(message_file)
    branch, head = branch_and_head(repo, expected_head)
    state = merge_state(repo)
    heads = state["merge_heads"]
    if not heads:
        raise WrapupError("no merge is in progress (no MERGE_HEAD); start one with `git merge --no-ff --no-commit`")
    if len(heads) != 1:
        raise WrapupError("MERGE_HEAD lists %d commits; only a two-parent merge is supported" % len(heads))
    if source and heads[0] != source:
        raise WrapupError("MERGE_HEAD is %s, not the expected source %s" % (heads[0][:12], source[:12]))
    if state["unmerged"]:
        raise WrapupError("unresolved conflicts: " + ", ".join(state["unmerged"]))
    if not state["tree"]:
        raise WrapupError("git write-tree failed, so the merge result cannot be compared with the reviewed tree")
    if expected_tree and state["tree"] != expected_tree:
        raise WrapupError("the index tree is %s, not the reviewed %s" % (state["tree"][:12], expected_tree[:12]))
    if not state["worktree_clean"]:
        raise WrapupError("tracked working files differ from the index; stage or restore them first")
    identity = parse_author(author) if author else head_identity(repo)
    return {"branch": branch, "head": head, "source": heads[0], "tree": state["tree"],
            "author": "%s <%s>" % identity, "identity": identity, "message": message}


def verify_merge(repo: Path, head: str, source: str, tree: str, identity: tuple[str, str]) -> str:
    """The merge commit that now exists must be exactly the one that was reviewed."""
    new = git_out(repo, "rev-parse", "HEAD")
    if new == head:
        raise WrapupError("HEAD did not move, so no merge commit was made")
    parents = git_out(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    if parents != [head, source]:
        raise WrapupError("commit %s has parents %s, not the planned [%s, %s]"
                          % (new[:12], [p[:12] for p in parents], head[:12], source[:12]))
    if git_out(repo, "rev-parse", "HEAD^{tree}") != tree:
        raise WrapupError("commit %s has a different tree from the one reviewed: a hook changed the index"
                          % new[:12])
    if merge_heads(repo):
        raise WrapupError("MERGE_HEAD is still present after the commit")
    check_identity(repo, new, identity)
    return new


def merge_commit(repo: Path, message_file: Path, author: str | None, expected_head: str | None,
                 source: str | None, expected_tree: str | None, apply: bool, timeout: float = 1800,
                 log: Path | None = None) -> dict[str, Any]:
    """Finish an unfinished merge through the real hook and check that the result is the reviewed merge.

    On any failure the merge is left exactly as it was, so a retry needs no redo."""
    ready = check_merge_ready(repo, message_file, author, expected_head, source, expected_tree)
    branch, head, tree, identity = ready["branch"], ready["head"], ready["tree"], ready["identity"]
    result: dict[str, Any] = {key: ready[key] for key in ("branch", "head", "source", "tree", "author")}
    print("merge commit on %s (HEAD %s + %s) as %s <%s>; tree %s"
          % (branch, head[:12], ready["source"][:12], *identity, tree[:12]))
    print("message: " + ready["message"].strip().splitlines()[0])
    if not apply:
        print("dry-run (no --apply): the merge stays uncommitted")
        return result
    if log is None:
        handle, name = tempfile.mkstemp(prefix="wrapup-merge-", suffix=".log")
        os.close(handle)
        log = Path(name)
    print("running the pre-commit hook (a full gate takes minutes); live output: " + str(log))
    code = run_commit(repo, message_file, commit_env(identity), log, timeout)
    if code != 0:
        tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise WrapupError("git commit exited %d, the merge is still uncommitted (log %s):\n%s" % (code, log, tail))
    result.update(commit=verify_merge(repo, head, ready["source"], tree, identity), log=str(log))
    print("committed merge %s on %s; tree %s matches the reviewed one" % (result["commit"][:12], branch, tree[:12]))
    return result


def remote_tip(repo: Path, url: str, ref: str) -> str:
    """SHA the remote reports for exactly this ref ('' when it has none)."""
    for line in git_out(repo, "ls-remote", url, ref).splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            return sha
    return ""


def push(repo: Path, branch: str, remote: str, apply: bool) -> dict[str, Any]:
    """Fast-forward the checked-out branch to the remote and read the tip back from the push URL."""
    current = git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if current != branch:
        raise WrapupError("the checked-out branch is %s, not %s; nothing was pushed"
                          % (current or "a detached HEAD", branch))
    head = git_out(repo, "rev-parse", "HEAD")
    url = git_out(repo, "remote", "get-url", "--push", remote)  # the push URL, not the fetch URL
    ref = "refs/heads/" + branch
    before = remote_tip(repo, url, ref)
    if not before:
        raise WrapupError("%s has no %s; a first push is not something this tool does" % (remote, branch))
    if before != head and git(repo, "merge-base", "--is-ancestor", before, head).returncode != 0:
        raise WrapupError("%s is at %s, which is not an ancestor of HEAD %s: behind or diverged, "
                          "and this tool never forces" % (remote, before[:12], head[:12]))
    count = int(git_out(repo, "rev-list", "--count", "%s..%s" % (before, head))) if before != head else 0
    result: dict[str, Any] = {"remote": redact(url), "before": before, "head": head, "commits": count}
    print("push %s -> %s (%s): %d commit(s)" % (branch, remote, redact(url), count))
    if count:
        print(git_out(repo, "log", "--oneline", "%s..%s" % (before, head)))
    if not apply or not count:
        print("dry-run (no --apply): nothing pushed" if not apply else "already up to date")
        return result
    pushed = git(repo, "push", remote, "%s:%s" % (ref, ref))  # no '+', no --force: a rejection stays a rejection
    if pushed.returncode != 0:
        raise WrapupError(redact("git push failed: " + pushed.stderr.strip()))
    after = remote_tip(repo, url, ref)
    if after != head:
        raise WrapupError("push reported success but the remote reads back %s, not %s"
                          % (after[:12] or "nothing", head[:12]))
    result["after"] = after
    print("pushed and read back from %s: remote tip %s == HEAD" % (redact(url), after[:12]))
    return result


def version_arg(text: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise argparse.ArgumentTypeError("expected X.Y.Z, got %r" % text)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    arch = sub.add_parser("archive", help="copy receipt-matched evidence into the repo archive")
    arch.add_argument("version", type=version_arg)
    arch.add_argument("--tree", required=True, type=Path, help="release tree holding the evidence")
    arch.add_argument("--repo", type=Path, default=Path.cwd(), help="repository to archive into")
    arch.add_argument("--apply", action="store_true")
    ret = sub.add_parser("retire", help="move a finished linked worktree to the Trash")
    ret.add_argument("--tree", required=True, type=Path)
    ret.add_argument("--repo", type=Path, default=Path.cwd())
    ret.add_argument("--base", default=DEFAULT_BASE, help="branch the tree must be merged into")
    ret.add_argument("--allow", action="append", default=[], help="exact untracked file that may be dropped")
    ret.add_argument("--trash", type=Path, default=Path.home() / ".Trash")
    ret.add_argument("--remote", action="store_true", help="also delete the merged branch on origin")
    ret.add_argument("--apply", action="store_true")
    com = sub.add_parser("commit", help="stage exactly the named files and commit through the real hook")
    com.add_argument("--path", action="append", required=True, help="exact file; repeat for each one")
    com.add_argument("-m", "--message-file", required=True, type=Path)
    com.add_argument("--repo", type=Path, default=Path.cwd())
    com.add_argument("--author", help="'Name <email>' for this commit only; default is HEAD's author")
    com.add_argument("--expected-head", help="full SHA the scope was approved against")
    com.add_argument("--timeout", type=float, default=1800, help="seconds the hook may take")
    com.add_argument("--log", type=Path, help="where the hook output goes (default: a temp file)")
    com.add_argument("--apply", action="store_true")
    psh = sub.add_parser("push", help="fast-forward push the checked-out branch and read it back")
    psh.add_argument("--repo", type=Path, default=Path.cwd())
    psh.add_argument("--branch", default=DEFAULT_BASE, help="must be the branch that is checked out")
    psh.add_argument("--to", default="origin", help="remote name")
    psh.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        repo = repo_root(args.repo)
        if args.command == "commit":
            commit(repo, args.path, args.message_file, args.author, args.expected_head, args.apply,
                   args.timeout, args.log)
        elif args.command == "push":
            push(repo, args.branch, args.to, args.apply)
        elif args.command == "archive":
            archive(repo, Path(os.path.realpath(args.tree)), args.version, args.apply)
        else:
            retire(repo, Path(os.path.realpath(args.tree)), args.base, args.allow, args.trash,
                   args.remote, args.apply)
    except WrapupError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
