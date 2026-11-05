"""§207 unattended runs: --stop-flow-at, --post-custom-script (bundled),
--auto-stop-idle (idle_stop.py --watch), janus_autorun.sh / janus_ec2.sh autorun.
Run: python3 -m pytest asic_flow/agent/tests/test_207_stop_flow_post_script_autostop.py -q"""
import inspect
import os
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest

HERE = os.path.dirname(__file__)
ROOT = Path(HERE).resolve().parents[2]
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "utils"))
import janus_agent as ja  # noqa: E402
import report_bundle as rb  # noqa: E402
import idle_stop as I  # noqa: E402


class _UI:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _agent(tmp_path, *argv):
    a = ja.JANUSAutonomousAgent.__new__(ja.JANUSAutonomousAgent)
    a.args = ja.build_parser().parse_args(list(argv))
    ja._apply_skips(a.args)
    a.flow_dir = str(tmp_path / "flow")
    os.makedirs(a.flow_dir, exist_ok=True)
    a.run_dir = str(tmp_path / "run")
    a.ui = _UI()
    a._make_cmd = types.MethodType(lambda self, blk: ["make", "DESIGN_CONFIG=c.mk"], a)
    a._post_build_make_cmd = types.MethodType(
        lambda self, blk, t: ["make", "DESIGN_CONFIG=c.mk", "-o", "x"] + list(t), a)
    for m in ("_results_dir", "_reports_dir", "_logs_dir"):
        setattr(a, m, types.MethodType(lambda self, blk: None, a))
    a._config_path = types.MethodType(lambda self, blk: "c.mk", a)
    return a


def _res(status="DONE"):
    return types.SimpleNamespace(status=status, note="n")


# ---------------------------------------------------------------- CLI ------
def test_parser_and_goal(tmp_path):
    a = _agent(tmp_path, "--stop-flow-at", "cts")
    assert a._stop_stage() == "cts" and a._stop_goal() == ["cts"]
    assert _agent(tmp_path)._stop_goal() == []
    with pytest.raises(SystemExit):
        ja.build_parser().parse_args(["--stop-flow-at", "finish"])
    # _run_make appends the goal to the build line only (post-build cmds keep theirs)
    src = inspect.getsource(ja.JANUSAutonomousAgent._run_make)
    assert "self._make_cmd(block) + self._stop_goal()" in src
    assert "_stop_goal" not in inspect.getsource(ja.JANUSAutonomousAgent._make_cmd)


def _val(*argv):
    args = ja.build_parser().parse_args(list(argv))
    ja._apply_skips(args)
    return ja._validate_args(args)


def test_validate(tmp_path):
    s = tmp_path / "p.sh"; s.write_text("true\n")
    assert _val("--stop-flow-at", "place", "--post-custom-script", str(s),
                "--post-custom-env", "SAP_MODE=split")[0] == []
    assert any("conflicts" in e for e in _val("--stop-flow-at", "cts", "--signoff-only")[0])
    assert any("after --stop-flow-at" in e for e in
               _val("--stop-flow-at", "cts", "--resume-from", "5_route")[0])
    assert _val("--stop-flow-at", "route", "--resume-from", "5_route")[0] == []
    assert any("not found" in e for e in _val("--post-custom-script", "/nope.tcl")[0])
    assert any("VAR=VAL" in e for e in
               _val("--post-custom-script", str(s), "--post-custom-env", "BAD")[0])
    assert any("no effect" in w for w in _val("--post-custom-args", "x")[1])
    assert any(">= 0" in e for e in _val("--auto-stop-idle", "-1")[0])


# ---------------------------------------------------- process / notify -----
def test_stop_branch_skips_post_route():
    src = inspect.getsource(ja.JANUSAutonomousAgent._process_block)
    i = src.index("stop = self._stop_stage()")
    j = src.index("self._classify_timing(block)")
    branch = src[i:j]
    assert 'res.status = "STOPPED"' in branch and "return res" in branch
    for later in ("_post_route_opt", "_run_abstract", "_run_signoff", "_archive_golden"):
        assert src.index(later) > i
    pm = inspect.getsource(ja.JANUSAutonomousAgent._process_and_mark)
    assert pm.index("_run_post_custom_script") < pm.index("self._notify_settled(block, res)")
    ns = inspect.getsource(ja.JANUSAutonomousAgent._notify_settled)
    assert "extra_dirs=self._bundle_extra_dirs(block)" in ns and "limit_stage=stop" in ns


def test_post_script_runs_with_env_and_is_bundled(tmp_path):
    s = tmp_path / "probe.sh"
    s.write_text('echo "blk=$JANUS_BLOCK st=$JANUS_BLOCK_STATUS m=$SAP_MODE a=$1"\n'
                 'echo probe-report > "$JANUS_POST_SCRIPT_OUT/probe.rpt"\n'
                 'mkdir -p "$JANUS_POST_SCRIPT_OUT/sub"; echo x > "$JANUS_POST_SCRIPT_OUT/sub/y.txt"\n')
    a = _agent(tmp_path, "--stop-flow-at", "cts", "--post-custom-script", str(s),
               "--post-custom-args", "hello", "--post-custom-env", "SAP_MODE=split")
    r = _res("STOPPED")
    assert a._run_post_custom_script("janus_soc_top", r) == 0
    out = Path(a._post_script_out("janus_soc_top"))
    log = (out / "post_custom_script.log").read_text()
    assert "blk=janus_soc_top st=STOPPED m=split a=hello" in log and "# rc=0" in log
    assert "post-script rc=0" in r.note
    assert a._bundle_extra_dirs("janus_soc_top") == {"post_script": str(out)}
    # bundle: post-script files included; later-stage stale reports skipped
    rep = tmp_path / "reports"; rep.mkdir()
    (rep / "4_cts_final.rpt").write_text("cts\n")
    (rep / "6_finish.rpt").write_text("stale finish\n")
    res = rb.build_bundle("janus_soc_top", "STOPPED", str(tmp_path / "bundle"),
                          logs_dir=None, reports_dir=str(rep), results_dir=None,
                          extra_dirs=a._bundle_extra_dirs("janus_soc_top"),
                          limit_stage="cts")
    with tarfile.open(res["tar"]) as t:
        names = t.getnames()
    assert any(n.endswith("post_script/probe.rpt") for n in names)
    assert any(n.endswith("post_script/sub/y.txt") for n in names)
    assert any(n.endswith("post_script/post_custom_script.log") for n in names)
    assert any(n.endswith("reports/4_cts_final.rpt") for n in names)
    assert not any(n.endswith("6_finish.rpt") for n in names)


def test_post_script_gating_timeout_and_tcl(tmp_path):
    s = tmp_path / "p.sh"; s.write_text("sleep 5\n")
    a = _agent(tmp_path, "--post-custom-script", str(s), "--post-custom-timeout", "0.5")
    assert a._run_post_custom_script("b", _res("FAILED")) is None       # gated
    assert a._run_post_custom_script("b", _res("DONE")) == 124           # timeout
    a.args.post_custom_on_fail = True
    assert a._run_post_custom_script("b", _res("FAILED")) == 124
    t = tmp_path / "probe.tcl"; t.write_text("puts hi\n")
    a2 = _agent(tmp_path, "--post-custom-script", str(t), "--post-custom-args", "SAP_MODE=split")
    cmd = a2._post_custom_cmd("b", str(t))
    assert cmd[:2] == ["make", "DESIGN_CONFIG=c.mk"] and "run" in cmd
    assert f"RUN_SCRIPT={t}" in cmd and cmd[-1] == "SAP_MODE=split"
    p = tmp_path / "x.py"; p.write_text("")
    assert a2._post_custom_cmd("b", str(p))[0] == sys.executable


def test_dry_run_and_no_script(tmp_path):
    assert _agent(tmp_path)._run_post_custom_script("b", _res()) is None
    s = tmp_path / "p.sh"; s.write_text("exit 3\n")
    a = _agent(tmp_path, "--post-custom-script", str(s), "--dry-run")
    assert a._run_post_custom_script("b", _res()) is None
    assert not os.path.isdir(a._post_script_out("b"))


# --------------------------------------------------------- auto-stop -------
def test_arm_auto_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JANUS_AUTO_STOP_IDLE", raising=False)
    calls = []
    fake = lambda cmd, **kw: calls.append((cmd, kw))  # noqa: E731
    args = ja.build_parser().parse_args([])
    assert ja._arm_auto_stop(args, popen=fake) is None and not calls
    args = ja.build_parser().parse_args(["--auto-stop-idle", "15", "--notify-email", "k@x"])
    cmd = ja._arm_auto_stop(args, popen=fake)
    assert cmd[-3:] == ["--watch", "--idle-minutes", "15"] and cmd[1].endswith("utils/idle_stop.py")
    assert os.path.isfile(cmd[1])
    kw = calls[0][1]
    assert kw["start_new_session"] and kw["env"]["JIS_NOTIFY_EMAIL"] == "k@x"
    monkeypatch.setenv("JANUS_AUTO_STOP_IDLE", "20")
    assert ja._arm_auto_stop(ja.build_parser().parse_args([]), popen=fake)[-1] == "20"
    assert ja._arm_auto_stop(ja.build_parser().parse_args(["--dry-run"]), popen=fake) is None
    assert "_arm_auto_stop(args)" in inspect.getsource(ja.main)


@pytest.fixture
def box(tmp_path, monkeypatch):
    proc, pts = tmp_path / "proc", tmp_path / "pts"
    proc.mkdir(); pts.mkdir()
    (proc / "loadavg").write_text("0.05 0.10 0.10 1/200 999\n")
    for k in list(os.environ):
        if k.startswith("JIS_"):
            monkeypatch.delenv(k)
    c = I.load_conf(None)
    c.update(PROC_ROOT=str(proc), PTS_DIR=str(pts), STATE_DIR=str(tmp_path / "st"),
             HOLD_FILE=str(tmp_path / "hold"), LOG_FILE=str(tmp_path / "log"),
             STOP_CMD="true", IDLE_MINUTES="15", WARN_MINUTES="5")
    return c, proc


def test_watch_loop_stops_after_idle(box, monkeypatch):
    c, proc = box
    clock = [1_800_000_000.0]
    monkeypatch.setattr(I.time, "time", lambda: clock[0])
    monkeypatch.setattr(I, "send_email", lambda *a: "email not configured")
    monkeypatch.setattr(I, "wall", lambda m: None)
    d = proc / "4242"; d.mkdir()
    (d / "cmdline").write_bytes(b"python3\0janus_agent.py\0--blocks\0x\0")

    def sleep(_s):
        clock[0] += 60
        if clock[0] > 1_800_000_000.0 + 600 and d.exists():   # agent exits at +10 min
            (d / "cmdline").unlink(); d.rmdir()
    rc, act = I.watch(c, interval=60, sleep=sleep, behavior_fn=lambda: "stop", max_ticks=200)
    assert (rc, act) == (0, "STOP")
    # stop happened >= 15 idle minutes after the agent vanished
    assert clock[0] - (1_800_000_000.0 + 600) >= 15 * 60
    assert not os.path.exists(os.path.join(c["STATE_DIR"], "watch.pid"))


def test_watch_singleton_and_refuse(box):
    c, _ = box
    os.makedirs(c["STATE_DIR"], exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                               "idle_stop.py"])
    try:
        Path(c["STATE_DIR"], "watch.pid").write_text(str(holder.pid))
        assert I.watch(c, max_ticks=1, sleep=lambda s: None) == (0, "ALREADY")
    finally:
        holder.kill(); holder.wait()
    c2 = dict(c, IDLE_MINUTES="0", WARN_MINUTES="0")
    I._state(c2, "last_busy", 1.0)
    assert I.watch(c2, sleep=lambda s: None, behavior_fn=lambda: "terminate") == (2, "REFUSED")


def test_watch_defaults_and_unreadable_conf(tmp_path, monkeypatch):
    for k in ("JIS_STATE_DIR", "JIS_LOG_FILE", "JIS_STOP_CMD"):
        monkeypatch.delenv(k, raising=False)
    c = I._watch_defaults(I.load_conf(None))
    assert c["STATE_DIR"].startswith("/tmp/janus-idle-watch-")
    if os.getuid() != 0:
        assert c["STOP_CMD"] == "sudo -n shutdown -h now"
        conf = tmp_path / "c.conf"; conf.write_text("IDLE_MINUTES=99\n"); conf.chmod(0)
        assert I.load_conf(str(conf))["IDLE_MINUTES"] == "30"     # no crash


# ------------------------------------------------------------ scripts ------
@pytest.mark.parametrize("rel", ["asic_flow/scripts/janus_autorun.sh",
                                 "asic_flow/scripts/janus_ec2.sh"])
def test_scripts_syntax(rel):
    assert subprocess.run(["bash", "-n", str(ROOT / rel)]).returncode == 0


def test_ec2_autorun_wired():
    s = (ROOT / "asic_flow/scripts/janus_ec2.sh").read_text()
    assert "autorun)      preflight; resolve_id; do_autorun" in s
    assert "ssh -n -o BatchMode=yes" in s and "janus_autorun.sh" in s
    r = (ROOT / "asic_flow/scripts/janus_autorun.sh").read_text()
    for needle in ("idle_stop.py\" --watch", "git pull --ff-only",
                   "03_setup_janus_configs.sh", "--auto-stop-idle", "flock -n 9"):
        assert needle in r


def test_autorun_no_pull_no_args(tmp_path):
    """No agent args -> exits 2 after arming nothing (IDLE_MIN=0)."""
    env = dict(os.environ, HOME=str(tmp_path), JANUS_AUTORUN_CONF=str(tmp_path / "none"))
    r = subprocess.run(["bash", str(ROOT / "asic_flow/scripts/janus_autorun.sh"),
                        "--no-pull", "--idle-min", "0", "--restamp", "never"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    log = (tmp_path / "janus_autorun" / "latest.log").read_text()
    assert "auto-stop disabled" in log and "no agent args" in log
