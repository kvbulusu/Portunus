"""§149 ara hold: post-route ECO via ORFS `make run` + HOLD margin 20->40.
Run: python3 -m pytest asic_flow/agent/tests/test_post_route_eco.py -q"""
import io
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "utils"))
import janus_agent as ja  # noqa: E402
import flow_state as fs  # noqa: E402
from incremental_opt import parse_eco, parse_eco_pre  # noqa: E402
from test_agent_adaptive import _agent  # noqa: E402

CFG = os.path.join(HERE, "..", "..", "configs")
TCL = os.path.join(HERE, "..", "..", "scripts", "post_route_opt.tcl")

GOOD_HOLD = """JANUS_PR loader orfs 6_final.odb
JANUS_PR_BEGIN hold
JANUS_PR_PRE setup wns -60.070
JANUS_PR_PRE hold wns -4.550
JANUS_PR drc 0
JANUS_PR setup wns -61.500
JANUS_PR setup tns -120.0
JANUS_PR hold wns 6.200
JANUS_PR hold tns 0.000
JANUS_PR wrote {odb}
JANUS_PR_END hold
"""


# ------------------------------------------------------------------ parsing --
def test_parse_eco_pre_and_post_are_separate():
    out = GOOD_HOLD.format(odb="/x/e.odb")
    assert parse_eco_pre(out) == (-60.07, -4.55)
    r = parse_eco(out, mode="hold")
    assert r.ok and r.setup_wns == -61.5 and r.hold_wns == 6.2 and r.drc == 0
    assert parse_eco_pre("JANUS_PR setup wns 1\n") == (None, None)


def test_parse_eco_error_line_wins():
    r = parse_eco("JANUS_PR_PRE hold wns -4\nJANUS_PR_ERROR drt: DRT-0206\n")
    assert not r.ok and "DRT-0206" in r.error


# ---------------------------------------------------------------- tcl guards --
def test_tcl_uses_incremental_grt_and_aborts_on_drt_failure():
    s = open(TCL).read()
    # repair happens INSIDE the GRT incremental window (dirty-net wire rip-up)
    i_start = s.index("_grt_start\n    set hm")
    i_rep = s.index("repair_timing -hold -hold_margin $hm")
    assert i_start < i_rep
    assert "global_route -end_incremental" in s
    assert "global_route -congestion_iterations 30" not in s   # from-scratch GRT gone
    assert 'puts "JANUS_PR_ERROR drt: $e"; exit 1' in s
    assert 'JANUS_PR_ERROR drt num_drvs' in s
    assert '"unbuffer,' not in s                                # SIGSEGV evidence


# ------------------------------------------------------------- lib discovery --
def test_orfs_lib_env_finds_nldm(tmp_path):
    a = _agent(tmp_path)
    orfs = tmp_path / "orfs"
    nldm = orfs / "flow" / "platforms" / "asap7" / "lib" / "NLDM"
    nldm.mkdir(parents=True)
    (nldm / "asap7sc7p5t_SEQ_RVT_FF_nldm_220123.lib").write_text("x")
    (nldm / "asap7sc7p5t_AO_RVT_FF_nldm_211120.lib.gz").write_text("x")
    a.orfs = str(orfs)
    a.design_root = str(tmp_path / "designs")
    a.sram_out_dir = str(tmp_path / "sram")
    a.rtl_root = None
    a._find_routed_odb = lambda b: None
    env = a._orfs_lib_env("user_core_ara")
    libs = env["LIB_FILES"].split()
    assert any("SEQ_RVT" in l for l in libs) and any(l.endswith(".lib.gz") for l in libs)


# ---------------------------------------------------------------- ECO runner --
def _eco_agent(tmp_path, *argv):
    a = _agent(tmp_path, *argv)
    res = tmp_path / "flow" / "results" / "asap7" / "user_core_ara" / "base"
    rpt = tmp_path / "flow" / "reports" / "asap7" / "user_core_ara" / "base"
    res.mkdir(parents=True)
    rpt.mkdir(parents=True)
    for k in ("odb", "spef", "def", "v", "gds", "sdc"):
        (res / f"6_final.{k}").write_text(f"orig-{k}")
    a.run_dir = str(tmp_path / "run")
    os.makedirs(a.run_dir)
    a._results_dir = lambda b: str(res)
    a._reports_dir = lambda b: str(rpt)
    a._post_build_make_cmd = lambda b, t: ["make", "-C", a.flow_dir] + list(t)
    a._janus_knob = lambda b, n: None
    a.results = {"user_core_ara": ja.BlockResult(block="user_core_ara",
                                                 wns_ns=-60.07, hold_ps=-4.55,
                                                 drc_violations=0)}
    os.makedirs(a.flow_dir, exist_ok=True)
    return a, res


class _FakeProc:
    def __init__(self, text, rc=0, outs=None):
        self.stdout = io.BytesIO(text.encode())
        self.returncode = rc
        self.pid = 999999
        for p in (outs or []):
            open(p, "w").write("eco")

    def wait(self):
        return self.returncode


def _patch_popen(monkeypatch, a, res, text, rc=0, write=True, seen=None):
    def fake(cmd, **kw):
        if seen is not None:
            seen.append(cmd)
        mode = next(t.split("=", 1)[1] for t in cmd if t.startswith("JANUS_PR_MODE="))
        outs = list(a._eco_outputs(str(res), mode).values()) if write else []
        return _FakeProc(text.format(odb=os.path.join(str(res), f"janus_eco_{mode}.odb")),
                         rc, outs)
    monkeypatch.setattr(ja.subprocess, "Popen", fake)


def _patch_gds(monkeypatch, res, rc=0):
    def fake_run(cmd, **kw):
        if rc == 0:
            (res / "6_1_merged.gds").write_text("merged-eco")
        return subprocess.CompletedProcess(cmd, rc)
    monkeypatch.setattr(ja.subprocess, "run", fake_run)


def test_run_eco_orfs_command(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    seen = []
    _patch_popen(monkeypatch, a, res, GOOD_HOLD, seen=seen)
    eco, outs = a._run_eco_orfs("user_core_ara", "hold")
    cmd = seen[0]
    assert "run" in cmd
    assert any(t.startswith("RUN_SCRIPT=") and t.endswith("post_route_opt.tcl") for t in cmd)
    for t in ("JANUS_PR_MODE=hold", "JANUS_PR_HOLD_MARGIN=10",
              "JANUS_PR_IN_ODB=6_final.odb", "JANUS_PR_IN_SDC=6_final.sdc",
              f"JANUS_SPEF={res}/6_final.spef", "RUN_LOG_NAME_STEM=janus_eco_hold"):
        assert t in cmd, t
    assert eco.ok and eco.pre_hold == -4.55 and eco.hold_wns == 6.2
    log = open(a._block_log("user_core_ara")).read()
    assert "janus_eco_hold" in log and "JANUS_PR hold wns 6.200" in log


def test_run_eco_orfs_nonzero_rc_is_untrusted(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, GOOD_HOLD, rc=139)
    eco, _ = a._run_eco_orfs("user_core_ara", "hold")
    assert not eco.ok and "rc=139" in eco.error


def test_hold_pass_adopted_swaps_all_and_remerges_gds(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, GOOD_HOLD)
    _patch_gds(monkeypatch, res)
    s, h = a._eco_pass_orfs("user_core_ara", "hold", -60.07, -4.55)
    # hold closed; setup -60.07 -> -61.5 is within the 5 ps hold-pass tolerance
    assert (s, h) == (-61.5, 6.2)
    for k in ("odb", "spef", "def", "v"):
        assert (res / f"6_final.{k}").read_text() == "eco"
        assert (res / f"6_final.{k}.preeco").read_text() == f"orig-{k}"
    assert (res / "6_final.gds").read_text() == "merged-eco"
    r = a.results["user_core_ara"]
    assert r.hold_ps == 6.2 and "+eco(hold)" in r.note
    assert "hold ECO ADOPTED" in open(a._block_log("user_core_ara")).read()


def test_gds_merge_failure_restores_everything(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, GOOD_HOLD)
    _patch_gds(monkeypatch, res, rc=2)
    s, h = a._eco_pass_orfs("user_core_ara", "hold", -60.07, -4.55)
    assert (s, h) == (-60.07, -4.55)
    for k in ("odb", "spef", "def", "v", "gds"):
        assert (res / f"6_final.{k}").read_text() == f"orig-{k}"


@pytest.mark.parametrize("text,why", [
    (GOOD_HOLD.replace("JANUS_PR hold wns 6.200", "JANUS_PR hold wns -9.000"), "HOLD WS regressed"),
    (GOOD_HOLD.replace("JANUS_PR drc 0", "JANUS_PR drc 7"), "left 7 DRC"),
    (GOOD_HOLD.replace("JANUS_PR drc 0\n", ""), "DRC count unknown"),
    (GOOD_HOLD.replace("setup wns -61.500", "setup wns -70.000"), "setup WNS regressed"),
    ("JANUS_PR_PRE hold wns -4.55\nJANUS_PR_ERROR drt: [ERROR DRT-0206]\n", "DRT-0206"),
])
def test_hold_pass_discarded_keeps_originals(tmp_path, monkeypatch, text, why):
    a, res = _eco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, text)
    _patch_gds(monkeypatch, res)
    s, h = a._eco_pass_orfs("user_core_ara", "hold", -60.07, -4.55)
    assert (s, h) == (-60.07, -4.55)
    for k in ("odb", "spef", "def", "v", "gds"):
        assert (res / f"6_final.{k}").read_text() == f"orig-{k}"
    assert not any(n.startswith("janus_eco_") for n in os.listdir(res))
    log = open(a._block_log("user_core_ara")).read()
    assert "hold ECO DISCARDED" in log and why in log


def test_setup_pass_that_opens_hold_is_discarded(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    # hold +0.5 -> -0.4: inside decide_timing's 1 ps noise band, so only the
    # dedicated "setup pass must never open hold" guard catches it
    txt = ("JANUS_PR_PRE setup wns -60.0\nJANUS_PR_PRE hold wns 0.5\nJANUS_PR drc 0\n"
           "JANUS_PR setup wns -40.0\nJANUS_PR hold wns -0.4\nJANUS_PR wrote {odb}\n")
    _patch_popen(monkeypatch, a, res, txt)
    _patch_gds(monkeypatch, res)
    assert a._eco_pass_orfs("user_core_ara", "setup", -60.0, 0.5) == (-60.0, 0.5)
    assert "opened hold" in open(a._block_log("user_core_ara")).read()


def test_post_route_opt_runs_hold_then_skips_setup_if_hold_open(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    a._find_routed_odb = lambda b: str(res / "6_final.odb")
    a._orfs_lib_env = lambda b: {}
    a.orfs = None
    calls = []

    def fake_pass(block, mode, s, h):
        calls.append(mode)
        return s, h                           # hold still open
    a._eco_pass_orfs = fake_pass
    a._post_route_opt("user_core_ara")
    assert calls == ["hold"]
    assert "setup ECO NOT attempted" in open(a._block_log("user_core_ara")).read()


# ------------------------------------------------------------------- config --
def test_ara_hold_margin_40_reruns_from_cts_only(tmp_path):
    d = tmp_path / "user_core_ara"
    d.mkdir()
    for n in ("user_core_ara.mk", "user_core_ara_local.mk"):
        (d / n).write_text(open(os.path.join(CFG, n)).read())
    new = fs.parse_config_vars(str(d / "user_core_ara.mk"))
    assert new["HOLD_SLACK_MARGIN"] == "40"
    old = dict(new, HOLD_SLACK_MARGIN="20", JANUS_ARA_HOLD_MARGIN_PS="20")
    keys = fs._changed_keys(old, new)
    assert keys == ["HOLD_SLACK_MARGIN"]
    assert fs._earliest_stage_for_keys(keys) == "4_cts"
