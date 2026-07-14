"""
git_ops.py — Minimal, safe git helper for the JANUS ASIC agent.

Why this exists
---------------
The user's requested workflow is:

    agent runs the flow  ->  a block fails on a REAL RTL/config bug  ->  agent
    asks the LLM  ->  applies the fix  ->  COMMITS the fix to git  ->  user does
    `git pull`, re-sources the block configs, and re-runs / --resume.

For that to work the fix must land in the *repository source* (so it survives a
re-stamp by scripts/03_setup_janus_configs.sh) and be captured by a git commit.
This module does only that, defensively:

  * It NEVER touches git unless commits are explicitly enabled.
  * It only ever `git add`s the exact files we changed (never `git add -A`).
  * It commits on the branch the user already has checked out — it does not
    create, switch, or delete branches.
  * `push` is OFF by default and, when on, pushes the current branch to
    `origin` using whatever credentials the user's machine already has. We
    never embed or read tokens here.
  * Any git failure is caught and reported; it never aborts the ASIC run. A
    failed commit/push just means the fix is still on disk for the user to
    commit manually.

Pure stdlib (subprocess). No network beyond an optional `git push`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass
class GitOps:
    repo_root: str
    logger: Any = None
    enable_commit: bool = True
    enable_push: bool = False
    # When non-empty, a committed fix is published to EVERY branch named here
    # (in addition to / instead of the current-branch push). This keeps mirror
    # branches that must stay identical — e.g. ["main", "v5.1_integration"] —
    # in lock-step: the new commit (HEAD) is pushed to each branch ref on
    # origin. Pushes are fast-forward only (never --force), so a branch that
    # has genuinely diverged is reported as a failure instead of being
    # clobbered. Empty list => fall back to a single current-branch push.
    sync_branches: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.log = self.logger or (lambda *a, **k: None)
        self._is_repo: Optional[bool] = None

    # ---- low-level runner ---------------------------------------------------
    def _git(self, *args: str, timeout: int = 60) -> Tuple[int, str]:
        """Run a git command in the repo; return (rc, combined_output)."""
        try:
            proc = subprocess.run(
                ["git", "-C", self.repo_root, *args],
                capture_output=True, text=True, timeout=timeout,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, f"git exec failed: {exc}"

    # ---- availability -------------------------------------------------------
    def available(self) -> bool:
        """True only if git exists AND repo_root is inside a work tree."""
        if self._is_repo is not None:
            return self._is_repo
        rc, out = self._git("rev-parse", "--is-inside-work-tree")
        self._is_repo = (rc == 0 and "true" in out)
        if not self._is_repo:
            self.log(f"[git] {self.repo_root} is not a git work tree; "
                     "auto-commit disabled")
        return self._is_repo

    def current_branch(self) -> str:
        rc, out = self._git("rev-parse", "--abbrev-ref", "HEAD")
        return out.strip() if rc == 0 else "(unknown)"

    def head_sha(self, short: bool = True) -> str:
        args = ("rev-parse", "--short", "HEAD") if short else ("rev-parse", "HEAD")
        rc, out = self._git(*args)
        return out.strip() if rc == 0 else "(unknown)"

    def head_subject(self) -> str:
        rc, out = self._git("log", "-1", "--pretty=%s")
        return out.strip() if rc == 0 else "(unknown)"

    def dirty_files(self) -> List[str]:
        """Tracked files with staged/unstaged modifications (excludes untracked
        artifacts). Empty list => working tree clean w.r.t. tracked sources."""
        rc, out = self._git("status", "--porcelain", "--untracked-files=no")
        if rc != 0:
            return []
        return [ln[3:].strip() for ln in out.splitlines() if ln.strip()]

    def provenance(self) -> str:
        """One-block, human-readable record of EXACTLY what source state a run
        was executed against. Stamped into every per-block log and the run
        manifest so any log shared for triage is self-identifying — no reliance
        on branch names or out-of-band memory of 'what was I running'."""
        if not self.available():
            return "GIT PROVENANCE: (not a git work tree)"
        dirty = self.dirty_files()
        lines = [
            "----- GIT PROVENANCE -------------------------------------------------",
            f"  branch      : {self.current_branch()}",
            f"  HEAD        : {self.head_sha(short=True)}  {self.head_subject()}",
            f"  tracked src : {'CLEAN' if not dirty else str(len(dirty)) + ' file(s) MODIFIED (uncommitted):'}",
        ]
        for f in dirty[:20]:
            lines.append(f"                  - {f}")
        if len(dirty) > 20:
            lines.append(f"                  ... (+{len(dirty) - 20} more)")
        lines.append("----------------------------------------------------------------------")
        return "\n".join(lines)

    def _identity_ok(self) -> bool:
        rc_n, _ = self._git("config", "user.name")
        rc_e, _ = self._git("config", "user.email")
        return rc_n == 0 and rc_e == 0

    # ---- commit -------------------------------------------------------------
    def commit_paths(self, paths: List[str], message: str) -> Tuple[bool, str]:
        """Stage exactly `paths` and commit them. Returns (ok, human_message).

        No-ops (ok=False with a reason) when commits are disabled, git is
        unavailable, or none of the paths are actually inside the repo /
        actually changed.
        """
        if not self.enable_commit:
            return False, "commit disabled (--no-git-commit)"
        if not self.available():
            return False, "not a git repo — fix left on disk, commit manually"
        # keep only real files inside the repo work tree
        inside: List[str] = []
        root = os.path.realpath(self.repo_root)
        for p in paths:
            if not p:
                continue
            rp = os.path.realpath(p)
            if rp.startswith(root) and os.path.exists(rp):
                inside.append(rp)
        if not inside:
            return False, "no in-repo files to commit"

        rc, out = self._git("add", "--", *inside)
        if rc != 0:
            return False, f"git add failed: {out.strip()[:200]}"

        # nothing staged (identical content)? then there is nothing to commit.
        rc_d, _ = self._git("diff", "--cached", "--quiet")
        if rc_d == 0:
            return False, "no staged changes (fix produced no diff)"

        if not self._identity_ok():
            # commit would fail with a cryptic error; give a clear instruction.
            return False, ("git user.name/user.email not set — cannot commit. "
                           "Run: git config user.name '...' && git config "
                           "user.email '...'  (fix is staged on disk).")

        rc, out = self._git("commit", "-m", message)
        if rc != 0:
            return False, f"git commit failed: {out.strip()[:200]}"
        # short hash for the log
        _, sha = self._git("rev-parse", "--short", "HEAD")
        return True, f"committed {sha.strip()} on {self.current_branch()}"

    # ---- push ---------------------------------------------------------------
    def push(self) -> Tuple[bool, str]:
        """Publish the just-made commit to origin (only if enabled).

        If `sync_branches` is set, the current HEAD is pushed to EACH of those
        branch refs on origin (keeping mirror branches like main +
        v5.1_integration identical). Otherwise the current branch is pushed.
        Returns (all_ok, combined_message). Never forces.
        """
        if not self.enable_push:
            return False, "push disabled (default; pass --git-push to enable)"
        if not self.available():
            return False, "not a git repo"
        if self.sync_branches:
            return self.push_branches(self.sync_branches)
        branch = self.current_branch()
        rc, out = self._git("push", "origin", branch, timeout=180)
        if rc != 0:
            return False, f"git push failed (credentials?): {out.strip()[:200]}"
        return True, f"pushed {branch} to origin"

    def push_branches(self, branches: List[str]) -> Tuple[bool, str]:
        """Push the current HEAD to each named branch ref on origin.

        Fast-forward only (no --force): if origin/<branch> has diverged from
        HEAD the push is rejected and reported, never clobbered. Returns
        (all_ok, per-branch summary). A branch already at HEAD counts as OK
        (git prints 'Everything up-to-date').
        """
        if not self.available():
            return False, "not a git repo"
        wanted = [b for b in dict.fromkeys(branches) if b]  # de-dup, keep order
        if not wanted:
            return False, "no branches to push"
        results: List[str] = []
        all_ok = True
        for b in wanted:
            # HEAD:refs/heads/<b> publishes whatever we just committed to that
            # branch regardless of which branch is checked out locally.
            rc, out = self._git("push", "origin", f"HEAD:refs/heads/{b}", timeout=180)
            if rc == 0:
                results.append(f"{b}✓")
            else:
                all_ok = False
                reason = out.strip().replace("\n", " ")[:120]
                # a non-fast-forward is the common, expected "diverged" case
                if "non-fast-forward" in out or "rejected" in out:
                    reason = "rejected (diverged — resolve manually, not forced)"
                results.append(f"{b}✗ [{reason}]")
        summary = "pushed HEAD -> " + ", ".join(results)
        return all_ok, summary


# ---- self-test (creates a throwaway repo) -----------------------------------
if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp()
    subprocess.run(["git", "-C", d, "init", "-q"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    f = os.path.join(d, "a.txt")
    open(f, "w").write("hello\n")
    g = GitOps(d, logger=print)
    assert g.available()
    ok, msg = g.commit_paths([f], "test: add a.txt")
    assert ok, msg
    print("git_ops self-test OK:", msg)
    # second commit with no change -> no-op
    ok2, msg2 = g.commit_paths([f], "noop")
    assert not ok2
    print("no-op path OK:", msg2)
