"""§205: janus_ec2.sh rewrites a Bitvise launcher (.cmd) on start/stop.

Runs the real script against a fake `aws` on PATH; no AWS/WSL needed.
"""
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "asic_flow" / "scripts" / "janus_ec2.sh"

FAKE_AWS = """#!/bin/bash
a="$*"; st=$(cat "$FAKE_STATE" 2>/dev/null || echo stopped)
case "$a" in
 *sts*) exit 0;;
 *start-instances*) echo running > "$FAKE_STATE";;
 *stop-instances*) echo stopped > "$FAKE_STATE";;
 *wait*) ;;
 *State.Name*) echo $st;;
 *InstanceType*) echo r5.4xlarge;;
 *PublicIpAddress*) [ $st = running ] && echo 3.91.22.7 || echo None;;
 *PrivateIpAddress*) echo 10.0.0.5;;
esac
"""


@pytest.fixture
def env(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    (b / "aws").write_text(FAKE_AWS)
    (b / "aws").chmod(0o755)
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    e = dict(os.environ)
    e.update(PATH=f"{b}:{e['PATH']}", HOME=str(home),
             FAKE_STATE=str(tmp_path / "state"), INSTANCE_ID="i-123",
             UPDATE_BITVISE="1", BITVISE_HOSTKEY_FP="off",
             BITVISE_LAUNCHER=str(tmp_path / "janus_bitvise.cmd"))
    return e, tmp_path / "janus_bitvise.cmd"


def run(e, action):
    return subprocess.run(["bash", str(SCRIPT), action], env=e,
                          capture_output=True, text=True, timeout=60)


def test_start_writes_launcher_with_ip(env):
    e, cmd = env
    r = run(e, "start")
    assert r.returncode == 0, r.stderr
    txt = cmd.read_bytes().decode()
    assert "\r\n" in txt
    assert "-host=3.91.22.7" in txt and "-loginOnStartup" in txt
    assert '-profile="%USERPROFILE%\\Documents\\janus.tlp"' in txt


def test_stop_marks_launcher_stopped(env):
    e, cmd = env
    assert run(e, "start").returncode == 0
    r = run(e, "stop")
    assert r.returncode == 0, r.stderr
    txt = cmd.read_text()
    assert "STOPPED" in txt and "-host=" not in txt


def test_disabled_and_non_wsl_are_noops(env):
    e, cmd = env
    assert run(dict(e, UPDATE_BITVISE="0"), "start").returncode == 0
    assert not cmd.exists()
    if not os.path.isdir("/mnt/c/Windows"):
        r = run(dict(e, UPDATE_BITVISE="auto", BITVISE_LAUNCHER=""), "status")
        assert r.returncode == 0, r.stderr
        assert not cmd.exists()
