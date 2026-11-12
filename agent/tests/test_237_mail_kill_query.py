"""§237 mail kill / query lines + box-side janus_remote.py + debug digest."""
import email
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UT = ROOT / "asic_flow/utils"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mt = _load("mail_trigger_237", UT / "mail_trigger.py")
jr = _load("janus_remote_237", UT / "janus_remote.py")


# ------------------------------------------------------------- parsing ---
def test_extract_actions_stops_at_quote():
    body = ("kill : now hold\nquery : hold 2\nquery: hist\ncmd : x\n"
            "On Tue someone wrote:\n> query : status\n> kill : now\n")
    k, q = mt.extract_actions(body)
    assert k == ["now", "hold"] and q == ["hold 2", "hist"]
    assert mt.extract_actions("cmd : asic_flow/agent/janus_agent.py --status") == (None, [])


def test_validate_kill():
    assert mt.validate_kill(["now"]) == (["kill"], None)
    assert mt.validate_kill(["now", "hold"])[0] == ["kill", "--hold"]
    assert mt.validate_kill(["dry"])[0] == ["kill", "--dry-run"]
    for bad in ([], ["everything"], ["now", "-9"]):
        assert mt.validate_kill(bad)[0] is None


@pytest.mark.parametrize("q,ok", [
    ("hold 2", True), ("hist", True), ("HIST 4_cts_pre_repair.rpt min 3", True),
    ('grep "RSZ-0032|JANUS_CTS" 4_1_cts.log 50', True), ("hooks 3_", True),
    ("tail ../../etc/passwd", False), ("grep x /etc/passwd", False),
    ("tail ~/.ssh/id_rsa", False), ("kill", False), ("rm -rf x", False),
    ("hold `id`", False), ('grep "unterminated 4_1_cts.log', False), ("", False)])
def test_validate_query(q, ok):
    a, why = mt.validate_query(q)
    assert (a is not None) == ok, why


# -------------------------------------------------------------- handle ---
def _mail(body, subj="[janus-lsf] go"):
    m = email.message.EmailMessage()
    m["Subject"], m["From"] = subj, "k@x.com"
    m.set_content(body)
    return email.message_from_bytes(m.as_bytes())


CFG = {"tag": "[janus-lsf]", "mode": "wsl2", "allow": "", "cooldown": 10, "reply_to": ""}
CMD = "cmd : asic_flow/agent/janus_agent.py --blocks janus_soc_top --resume-from 3_place"


@pytest.fixture
def fake(monkeypatch):
    calls = {"remote": [], "launch": []}
    res = {"kill_ok": True}
    monkeypatch.setattr(mt, "remote_argv", lambda a, m: ["remote"] + a)
    monkeypatch.setattr(mt, "launch_argv", lambda a, m: ["launch"] + a)
    monkeypatch.setattr(mt, "validate_cmd", lambda c, parser="auto": (c.split()[1:], None) if c else (None, "no cmd"))

    def remote(a, m):
        calls["remote"].append(a)
        return (res["kill_ok"] if a[0] == "kill" else True), f"out {a[0]}"

    def launch(a, m):
        calls["launch"].append(a)
        return True, "pid 1"
    monkeypatch.setattr(mt, "remote", remote)
    monkeypatch.setattr(mt, "launch", launch)
    return calls, res


def test_kill_then_cmd_skips_cooldown(fake):
    calls, _ = fake
    import time
    st = {"last_launch": time.time()}          # inside cooldown
    s, d = mt.handle(_mail("kill : now\n" + CMD), CFG, st)
    assert s == "done", d
    assert calls["remote"] == [["kill"]] and len(calls["launch"]) == 1


def test_cmd_alone_still_cooldown(fake):
    calls, _ = fake
    import time
    s, _ = mt.handle(_mail(CMD), CFG, {"last_launch": time.time()})
    assert s == "reject" and not calls["launch"]


def test_failed_kill_no_launch(fake):
    calls, res = fake
    res["kill_ok"] = False
    s, d = mt.handle(_mail("kill : now\n" + CMD), CFG, {})
    assert s == "error" and not calls["launch"] and "NOT launched" in d


def test_bad_query_runs_nothing(fake):
    calls, _ = fake
    s, _ = mt.handle(_mail("kill : now\nquery : tail ../x\n" + CMD), CFG, {})
    assert s == "reject" and not calls["remote"] and not calls["launch"]


def test_queries_only_and_dry(fake):
    calls, _ = fake
    s, d = mt.handle(_mail("query : hold 2\nquery : hist\n"), CFG, {})
    assert s == "done" and calls["remote"] == [["hold", "2"], ["hist"]] and "out hist" in d
    calls["remote"].clear()
    s, d = mt.handle(_mail("kill : now\nquery : status\n" + CMD), CFG, {}, dry=True)
    assert s == "dry" and not calls["remote"] and not calls["launch"]
    assert "would run: remote kill" in d


def test_remote_argv_real():
    a = mt.remote_argv(["hold", "2"], "local") if mt.REPO else None
    if a:
        assert a[-3].endswith("janus_remote.py") and a[-2:] == ["hold", "2"]


# --------------------------------------------------------- janus_remote ---
def _path(start, end, slack, ptype="min", nbuf=6):
    rows = [f"  10.00 {10 + 10 * i:7.2f} ^ janus_malpushbuf_{i}/Y (BUFx24_ASAP7_75t_R)"
            for i in range(nbuf)]
    return "\n".join([
        f"Startpoint: {start} (rising edge-triggered flip-flop clocked by clk)",
        f"Endpoint: {end} (rising edge-triggered flip-flop clocked by clk)",
        "Path Group: clk", f"Path Type: {ptype}", "Corner: ff", "",
        "  Delay    Time   Description", "-" * 40,
        "   0.00    0.00   clock clk (rise edge)", *rows,
        "                  janus_malpushbuf_0/Y (net)",
        f"  20.00  200.00 ^ {end} (DFFHQNx1_ASAP7_75t_R)",
        "         200.00   data arrival time", "",
        f"  {slack:7.2f}   slack ({'VIOLATED' if slack < 0 else 'MET'})", "", ""])


@pytest.fixture
def flow(tmp_path, monkeypatch):
    rep = tmp_path / "flow/reports/asap7/janus_soc_top/base"
    logs = tmp_path / "flow/logs/asap7/janus_soc_top/base"
    rep.mkdir(parents=True)
    logs.mkdir(parents=True)
    (rep / "4_cts_pre_repair.rpt").write_text(
        _path("u_ampo/a_reg/CLK", "u_user/x_reg/D", -42.5)
        + _path("u_ampo/b_reg/CLK", "u_user/y_reg/D", -10.0)
        + _path("u_noc/c_reg/CLK", "u_noc/d_reg/D", -3.0)
        + _path("u_noc/c_reg/CLK", "u_noc/e_reg/D", 50.0, ptype="max"))
    (logs / "4_1_cts.log").write_text("x\nJANUS_CTS foo\n[WARNING RSZ-0032] bar\nJANUS_AGBAL ok\n")
    monkeypatch.setenv("JANUS_FLOW_DIR", str(tmp_path / "flow"))
    monkeypatch.delenv("ORFS_DIR", raising=False)
    return tmp_path


def test_split_and_condense(flow):
    ps = jr.split_paths((flow / "flow/reports/asap7/janus_soc_top/base/4_cts_pre_repair.rpt").read_text())
    assert len(ps) == 4 and ps[0]["slack"] == -42.5 and ps[0]["type"] == "min"
    c = jr.condense(ps[0]["lines"])
    assert not any(l.endswith("(net)") for l in c)
    assert any("more janus_malpushbuf_#" in l and "run 6 pins" in l for l in c)


def test_hold_hist_grep_hooks(flow):
    out = jr.run(["hold", "2"])
    assert "3 min path(s), showing 2" in out and "-42.5" in out and "-3.0" not in out
    h = jr.run(["hist"]).split("-- startpoints")
    assert "u_user/x_reg" in h[0] and "u_ampo/a_reg" in h[1]
    h1 = jr.run(["hist", "4_cts_pre_repair.rpt", "min", "1"])
    assert "      2      -42.5  u_user" in h1 and "      1       -3.0  u_noc" in h1
    assert "2 match" in jr.run(["grep", "RSZ-0032|JANUS_CTS", "4_1_cts.log"])
    assert "JANUS_AGBAL ok" in jr.run(["hooks", "4_"])
    assert "JANUS_CTS foo" in jr.run(["tail", "4_1_cts.log", "5"])
    assert "4_cts_pre_repair.rpt" in jr.run(["ls"])


@pytest.mark.parametrize("argv", [["tail", "../x"], ["grep", "a", "/etc/passwd"],
                                  ["tail", ".bashrc"], ["hold", "999"], ["nope"],
                                  ["kill", "--force"], ["grep", "(", "4_1_cts.log"]])
def test_remote_rejects(flow, argv, capsys):
    assert jr.main(argv) == 2
    assert capsys.readouterr().out.startswith("ERROR:")


def test_kill_dry_and_self_exclusion(monkeypatch):
    me = os.getpid()
    rows = [(111, 111, 60, "bash /home/ubuntu/janus_autorun/x.run.sh"),
            (112, 111, 60, "python3 asic_flow/agent/janus_agent.py --blocks janus_soc_top"),
            (113, 113, 60, "/usr/bin/openroad -exit 4_1_cts.tcl"),
            (114, 114, 60, "python3 /home/ubuntu/idle_stop.py"),
            (115, 115, 60, "vim notes.txt"),
            (me, os.getpgrp(), 1, "python3 - kill")]
    monkeypatch.setattr(jr, "ps_rows", lambda: rows)
    monkeypatch.setattr(jr, "tmux_sessions", lambda: ["janus-autorun-1"])
    killed = []
    monkeypatch.setattr(jr.os, "killpg", lambda g, s: killed.append(g))
    out = jr.run(["kill", "--dry-run"])
    assert "pid     111" in out and "pid     113" in out and "idle_stop" not in out
    assert "vim" not in out and "dry-run" in out and not killed
    assert [r[0] for r in jr.flow_procs()] == [111, 112, 113]


# -------------------------------------------------------------- scripts ---
def test_ec2_sh_remote_dispatch():
    sh = ROOT / "asic_flow/scripts/janus_ec2.sh"
    t = sh.read_text()
    assert "do_remote(){" in t and "remote)       preflight; resolve_id; do_remote" in t
    assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0


def test_digest_script(flow, tmp_path):
    sh = ROOT / "asic_flow/scripts/debug/janus_debug_digest.sh"
    assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0
    out = tmp_path / "pso"
    out.mkdir()
    env = dict(os.environ, JANUS_POST_SCRIPT_OUT=str(out), JANUS_BLOCK="janus_soc_top",
               JANUS_DIGEST="hold 1;hist;kill;tail nope.log")
    r = subprocess.run(["bash", str(sh)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    d = (out / "debug_digest.txt").read_text()
    assert "-42.5" in d and "-- endpoints" in d and "kill not allowed" in d
    assert "ERROR: nope.log: not found" in d
