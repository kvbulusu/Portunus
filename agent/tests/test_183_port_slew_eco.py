"""§183 user_domain: post-route long output-port wire max-slew ECO (mode=drv).
Run: python3 -m pytest asic_flow/agent/tests/test_183_port_slew_eco.py -q"""
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import janus_agent as ja  # noqa: E402
from incremental_opt import parse_eco, decide_drv, ECOResult  # noqa: E402
from test_post_route_eco import _eco_agent, _patch_popen, _patch_gds  # noqa: E402

TCL = os.path.join(HERE, "..", "..", "scripts", "post_route_opt.tcl")
BLK = "user_core_ara"      # fixture block name (results dir is mocked)

DRV_OUT = """JANUS_PR_BEGIN drv
JANUS_PR_PRE setup wns 67.270
JANUS_PR_PRE hold wns 17.400
JANUS_PR_PRE drv slew 95
JANUS_PR drv scope ports nets=762 frozen=40000
JANUS_PR drv_repair max_wire_length 160 um
JANUS_PR drc 0
JANUS_PR spef /x/janus_eco_drv.spef
JANUS_PR setup wns {setup}
JANUS_PR setup tns 0.000
JANUS_PR hold wns {hold}
JANUS_PR hold tns 0.000
JANUS_PR drv slew {slew}
JANUS_PR wrote {{odb}}
JANUS_PR_END drv
"""


def _out(setup="66.000", hold="17.100", slew="18"):
    return DRV_OUT.format(setup=setup, hold=hold, slew=slew)


# ------------------------------------------------------------------ parsing --
def test_parse_drv_slew_counts():
    r = parse_eco(_out().format(odb="/x/e.odb"), mode="drv")
    assert r.ok and r.pre_slew_viol == 95 and r.slew_viol == 18 and r.drc == 0
    r2 = parse_eco("JANUS_PR_PRE drv slew -1\nJANUS_PR setup wns 1\nJANUS_PR wrote x\n")
    assert r2.pre_slew_viol is None and r2.slew_viol is None
    r3 = parse_eco("JANUS_PR_PRE drv slew 95\nJANUS_PR_ERROR repair_design: boom\n")
    assert not r3.ok and r3.pre_slew_viol == 95 and "boom" in r3.error


# ----------------------------------------------------------------- decision --
def _r(setup=66.0, hold=17.1, drc=0, pre=95, post=18, ok=True):
    return ECOResult(ok=ok, mode="drv", setup_wns=setup, hold_wns=hold, drc=drc,
                     pre_slew_viol=pre, slew_viol=post, wrote=ok)


@pytest.mark.parametrize("kw,verdict,frag", [
    ({}, "adopt", "95 -> 18"),
    ({"ok": False}, "discard", "no trustworthy"),
    ({"hold": -0.5}, "discard", "hold"),
    ({"hold": 15.0}, "discard", "hold regressed"),
    ({"setup": 60.0}, "discard", "setup regressed"),
    ({"drc": 3}, "discard", "DRC"),
    ({"post": 95}, "discard", "not reduced"),
    ({"post": None}, "discard", "unknown"),
])
def test_decide_drv(kw, verdict, frag):
    v, why = decide_drv(67.27, 17.4, 0, _r(**kw))
    assert v == verdict and frag in why, why


def test_decide_drv_setup_must_not_reopen():
    v, why = decide_drv(2.0, 17.4, 0, _r(setup=-1.0))
    assert v == "discard" and "reopened" in why


# ------------------------------------------------------------- agent wiring --
def test_run_eco_orfs_drv_targets(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    seen = []
    _patch_popen(monkeypatch, a, res, _out(), seen=seen)
    eco, _ = a._run_eco_orfs(BLK, "drv")
    cmd = seen[0]
    assert "JANUS_PR_MODE=drv" in cmd
    assert "JANUS_PR_DRV_MAX_WIRE_UM=160" in cmd and "JANUS_PR_DRV_SCOPE=ports" in cmd
    assert eco.ok and eco.slew_viol == 18


def test_run_eco_orfs_drv_knobs(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    knobs = {"JANUS_ECO_DRV_MAX_WIRE_UM": "120", "JANUS_ECO_DRV_SCOPE": "all"}
    a._janus_knob = lambda b, n: knobs.get(n)
    seen = []
    _patch_popen(monkeypatch, a, res, _out(), seen=seen)
    a._run_eco_orfs(BLK, "drv")
    assert "JANUS_PR_DRV_MAX_WIRE_UM=120" in seen[0] and "JANUS_PR_DRV_SCOPE=all" in seen[0]
    knobs.update(JANUS_ECO_DRV_MAX_WIRE_UM="x", JANUS_ECO_DRV_SCOPE="bogus")
    assert a._eco_drv_max_wire_um(BLK) == "160" and a._eco_drv_scope(BLK) == "ports"


def test_hold_mode_has_no_drv_targets(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    seen = []
    _patch_popen(monkeypatch, a, res, _out(), seen=seen)
    a._run_eco_orfs(BLK, "hold")
    assert not any(t.startswith("JANUS_PR_DRV_") for t in seen[0])


def _closed_agent(tmp_path):
    a, res = _eco_agent(tmp_path)
    r = a.results[BLK]
    r.wns_ns, r.hold_ps, r.drc_violations = 67.27, 17.4, 0
    return a, res


def test_drv_pass_adopts(tmp_path, monkeypatch):
    a, res = _closed_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, _out())
    _patch_gds(monkeypatch, res)
    assert a._eco_drv_pass(BLK) is True
    assert (res / "6_final.odb").read_text() == "eco"
    assert (res / "6_final.odb.preeco").read_text() == "orig-odb"
    assert "+eco(drv) slew 95->18" in a.results[BLK].note
    assert a.results[BLK].wns_ns == 66.0


def test_drv_pass_discards_on_hold_regression(tmp_path, monkeypatch):
    a, res = _closed_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, _out(hold="10.0"))
    assert a._eco_drv_pass(BLK) is False
    assert (res / "6_final.odb").read_text() == "orig-odb"
    assert not (res / "janus_eco_drv.odb").exists()


def test_eco_drv_only_dispatch_and_flags(tmp_path, monkeypatch):
    a, res = _closed_agent(tmp_path)
    a.args.eco_drv_only, a.args.blocks = True, [BLK]
    a._find_routed_odb = lambda b: str(res / "6_final.odb")
    a._classify_timing = lambda b: None
    calls = []
    a._eco_drv_pass = lambda b: calls.append("drv") or True
    a._run_abstract = lambda b: calls.append("abs")
    a._run_signoff = lambda b: calls.append("so") or "ok"
    a._archive_golden = lambda b, r=None: calls.append("gold")
    assert a.run() == 0 and calls == ["drv", "abs", "so", "gold"]
    calls.clear()
    a._eco_drv_pass = lambda b: calls.append("drv") and False
    assert a._eco_drv_only() == 5 and calls == ["drv"]
    p = ja.build_parser()
    ns = p.parse_args(["--eco-drv-only", "--blocks", "user_domain"])
    assert ns.eco_drv_only and not ns.eco_drv
    assert "eco_drv_only" in ja._EXCLUSIVE_MODES


def test_post_route_opt_runs_drv_stage_only_when_opted_in(tmp_path):
    a, _ = _closed_agent(tmp_path)
    calls = []
    a._post_route_opt_timing = lambda b: calls.append("t")
    a._eco_drv_pass = lambda b: calls.append("d")
    a.args.eco_drv = False
    a._post_route_opt(BLK)
    a.args.eco_drv = True
    a._post_route_opt(BLK)
    assert calls == ["t", "t", "d"]


# ---------------------------------------------------------------- tcl guards --
def test_tcl_drv_branch():
    s = open(TCL).read()
    i = s.index('} elseif {$mode eq "drv"} {')
    body = s[i:s.index('} elseif {$mode eq "drc"} {')]
    assert "estimate_parasitics -placement" in body          # §166: never on SPEF
    assert body.index("_grt_start") < body.index("repair_design -max_wire_length")
    assert body.index("foreach n $frozen { $n setDoNotTouch 0 }") < body.index("_reroute")
    assert "isDoNotTouch" in body                             # §177 detours untouched
    assert 'JANUS_PR_PRE drv slew' in body and 'JANUS_PR drv slew' in body
    assert "$mode in {hold setup timing drv}" in s            # def/v side outputs


@pytest.mark.skipif(not shutil.which("tclsh"), reason="no tclsh")
def test_tcl_is_complete():
    script = f'set f [open {{{TCL}}}]; set s [read $f]; close $f; puts [info complete $s]'
    out = subprocess.run(["tclsh"], input=script, capture_output=True, text=True).stdout
    assert out.strip() == "1"
