"""§196 EC2 idle auto-stop watchdog: decision logic on a fake /proc + /dev/pts."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
import idle_stop as I  # noqa: E402

T0 = 1_800_000_000.0


@pytest.fixture
def box(tmp_path, monkeypatch):
    proc, pts, st = tmp_path / "proc", tmp_path / "pts", tmp_path / "state"
    proc.mkdir(); pts.mkdir()
    (proc / "loadavg").write_text("0.05 0.10 0.10 1/200 999\n")
    for k in list(os.environ):
        if k.startswith("JIS_"):
            monkeypatch.delenv(k)
    c = I.load_conf(None)
    c.update(PROC_ROOT=str(proc), PTS_DIR=str(pts), STATE_DIR=str(st),
             HOLD_FILE=str(tmp_path / "hold"), LOG_FILE=str(tmp_path / "log"),
             STOP_CMD="false")
    return c, proc, pts, tmp_path


def _proc(proc, pid, *argv):
    d = proc / str(pid); d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


def _never(): raise AssertionError("behavior_fn must not be called before the idle limit")


def test_grace_then_warn_then_stop(box):
    c, *_ = box
    assert I.check(c, T0, True, _never)[0] == "BUSY"            # first tick = boot grace
    assert I.check(c, T0 + 10 * 60, True, _never)[0] == "IDLE"
    assert I.check(c, T0 + 26 * 60, True, _never)[0] == "WARN"
    act, det = I.check(c, T0 + 31 * 60, True, lambda: "stop")
    assert act == "STOP" and "dry-run" in det


@pytest.mark.parametrize("argv", [
    ("python3", "asic_flow/agent/janus_agent.py", "run", "--block", "janus_soc_top"),
    ("/usr/bin/openroad", "-exit", "cts.tcl"),
    ("make", "DESIGN_CONFIG=designs/asap7/janus_soc_top/config.mk", "do-4_1_cts"),
    ("/usr/bin/yosys", "-c", "synth.tcl"),
    ("rsync", "-a", "x", "y"),
])
def test_flow_process_keeps_busy(box, argv):
    c, proc, *_ = box
    I.check(c, T0, True, _never)
    _proc(proc, 4242, *argv)
    act, det = I.check(c, T0 + 40 * 60, True, _never)
    assert act == "BUSY" and "4242" in det


def test_unrelated_process_and_self_ignored(box):
    c, proc, *_ = box
    _proc(proc, 11, "/usr/sbin/sshd", "-D")
    _proc(proc, 12, "/usr/bin/python3", "/usr/local/lib/janus/idle_stop.py")
    _proc(proc, 13, "bash", "-c", "status")   # 'sta' must not match inside words
    assert I.busy_processes(c["PROC_ROOT"], I.BUSY_PATTERNS) == []


def test_hold_file_terminal_and_load(box):
    c, proc, pts, tmp = box
    I.check(c, T0, True, _never)
    (tmp / "hold").touch()
    assert "hold file" in I.check(c, T0 + 40 * 60, True, _never)[1]
    (tmp / "hold").unlink()
    t = pts / "0"; t.touch(); os.utime(t, (T0 + 75 * 60, T0 + 75 * 60))   # typed 5 min before
    assert "terminal input" in I.check(c, T0 + 80 * 60, True, _never)[1]
    os.utime(t, (T0, T0))                                                # old pty: not busy
    (proc / "loadavg").write_text("3.5 3.0 2.0 1/200 9\n")
    assert "load5" in I.check(c, T0 + 120 * 60, True, _never)[1]


def test_busy_resets_idle_clock(box):
    c, proc, *_ = box
    I.check(c, T0, True, _never)
    assert I.check(c, T0 + 26 * 60, True, _never)[0] == "WARN"
    _proc(proc, 77, "openroad", "-exit", "route.tcl")
    assert I.check(c, T0 + 27 * 60, True, _never)[0] == "BUSY"
    (proc / "77" / "cmdline").unlink(); (proc / "77").rmdir()
    assert I.check(c, T0 + 50 * 60, True, _never)[0] == "IDLE"   # 23 min since last busy


def test_terminate_behavior_refused(box):
    c, *_ = box
    I.check(c, T0, True, _never)
    act, det = I.check(c, T0 + 31 * 60, True, lambda: "terminate")
    assert act == "REFUSED" and "idle-alarm" in det
    c["ALLOW_TERMINATE"] = "1"
    assert I.check(c, T0 + 32 * 60, True, lambda: "terminate")[0] == "STOP"


def test_disabled_and_conf_parsing(box, tmp_path, monkeypatch):
    c, *_ = box
    f = tmp_path / "c.conf"
    f.write_text("# x\nIDLE_MINUTES=45\nexport SMTP_PASS='a b'\nENABLED=0\n")
    monkeypatch.setenv("JIS_LOAD_MAX", "2.5")
    cc = I.load_conf(str(f))
    assert (cc["IDLE_MINUTES"], cc["SMTP_PASS"], cc["ENABLED"], cc["LOAD_MAX"]) == ("45", "a b", "0", "2.5")
    c["ENABLED"] = "0"
    assert I.check(c, T0, True, _never)[0] == "DISABLED"


def test_real_stop_runs_cmd_once_and_logs(box, monkeypatch):
    c, _, _, tmp = box
    marker = tmp / "stopped"
    c["STOP_CMD"] = f"touch {marker}"
    monkeypatch.setattr(I, "wall", lambda m: None)
    I.check(c, T0, False, _never)
    assert I.check(c, T0 + 31 * 60, False, lambda: "stop")[0] == "STOP"
    assert marker.exists() and "STOP" in (tmp / "log").read_text()
    marker.unlink()
    assert I.check(c, T0 + 32 * 60, False, lambda: "stop")[0] == "IDLE"   # clock reset, no loop


def test_scripts_parse():
    s = ROOT / "asic_flow" / "scripts"
    for f in ("install_idle_stop.sh", "janus_ec2.sh"):
        subprocess.run(["bash", "-n", str(s / f)], check=True)
    ec2 = (s / "janus_ec2.sh").read_text()
    assert "idle-alarm)" in ec2 and "arn:aws:automate:$REGION:ec2:stop" in ec2
    assert "launch-spot)" in ec2 and '"InstanceInterruptionBehavior":"stop"' in ec2
    svc = (s / "systemd" / "janus-idle-stop.service").read_text()
    assert "/usr/local/lib/janus/idle_stop.py" in svc
    assert "OnUnitActiveSec=5min" in (s / "systemd" / "janus-idle-stop.timer").read_text()
