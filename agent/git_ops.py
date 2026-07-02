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
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple


@dataclass
class GitOps:
    repo_root: str
    logger: Any = None
    enable_commit: bool = True
    enable_push: bool = False

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
        """Push the current branch to origin (only if enabled)."""
        if not self.enable_push:
            return False, "push disabled (default; pass --git-push to enable)"
        if not self.available():
            return False, "not a git repo"
        branch = self.current_branch()
        rc, out = self._git("push", "origin", branch, timeout=180)
        if rc != 0:
            return False, f"git push failed (credentials?): {out.strip()[:200]}"
        return True, f"pushed {branch} to origin"


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
