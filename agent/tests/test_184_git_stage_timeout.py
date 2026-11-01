"""§184: golden git add/commit timeout + stale index.lock cleanup."""
import os
import subprocess
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import git_ops  # noqa: E402


def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.email", "t@t"], check=True)
    return r


def _make(r):
    g = git_ops.GitOps(repo_root=str(r), enable_commit=True)
    assert g.available()              # cache before _git is mocked
    return g


def test_stage_timeout_is_long_and_add_uses_it(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    g = _make(r)
    assert g._STAGE_TIMEOUT_S >= 600
    seen = []
    real = g._git

    def spy(*a, timeout=60):
        seen.append((a[0], timeout))
        return real(*a, timeout=timeout)
    monkeypatch.setattr(g, "_git", spy)
    (r / "f.txt").write_text("x")
    ok, msg = g.commit_paths([str(r / "f.txt")], "m")
    assert ok, msg
    assert ("add", g._STAGE_TIMEOUT_S) in seen and ("commit", g._STAGE_TIMEOUT_S) in seen


def test_killed_add_removes_its_own_lock(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    g = _make(r)
    lock = r / ".git" / "index.lock"

    def killed(*a, timeout=60):
        lock.write_text("")            # what a killed git add leaves behind
        return 127, "git exec failed: Command '[git add]' timed out after 60 seconds"
    monkeypatch.setattr(g, "_git", killed)
    (r / "f.txt").write_text("x")
    ok, msg = g.commit_paths([str(r / "f.txt")], "m")
    assert not ok and "removed stale .git/index.lock" in msg and not lock.exists()


def test_preexisting_lock_is_never_removed(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    g = _make(r)
    lock = r / ".git" / "index.lock"
    lock.write_text("")                # another git process owns it
    monkeypatch.setattr(g, "_git", lambda *a, timeout=60: (127, "git exec failed"))
    (r / "f.txt").write_text("x")
    ok, _ = g.commit_paths([str(r / "f.txt")], "m")
    assert not ok and lock.exists()
