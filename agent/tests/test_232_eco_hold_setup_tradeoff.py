"""§232: post-route hold-ECO hold-vs-setup exchange rule + setup recovery gate.
§233: _janus_knob reads `export NAME = v` lines (block mk files use export)."""
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import janus_agent as ja  # noqa: E402
from incremental_opt import ECOResult, parse_eco  # noqa: E402

A = ja.JANUSAutonomousAgent
KN = dict(A._ECO_TRADEOFF_DEFAULTS, JANUS_ECO_HOLD_MIN_GAIN_PS=20.0)
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def _eco(**kw):
    e = ECOResult(ok=True, mode="hold", drc=0)
    for k, v in kw.items():
        setattr(e, k, v)
    return e


@pytest.mark.parametrize("b_setup,allow", [
    (-139.94, 5.0), (-80.0, 5.0), (-60.0, 20.0), (-20.0, 40.0), (10.0, 40.0)])
def test_allowance(b_setup, allow):
    assert A._eco_setup_allowance(b_setup, KN) == pytest.approx(allow)


R210551 = dict(setup_wns=-142.621, setup_tns=-2475.835, hold_wns=-120.734,
               hold_tns=-645.911, pre_setup_tns=-2513.503, pre_hold_tns=-31427.188)


@pytest.mark.parametrize("kw,b_setup,b_hold,ok,frag", [
    (R210551, -139.941, -193.439, True, "IMPROVED"),
    # gain 13 ps < 20, hold not closed
    (dict(R210551, hold_wns=-180.0), -139.941, -193.439, False, "min 20"),
    # setup beyond -80: only the 5 ps floor
    (dict(R210551, setup_wns=-146.0), -139.941, -193.439, False, "allowance 5.00"),
    # setup -20: 40 ps budget; 30 ps loss needs >= 60 ps gain
    (dict(setup_wns=-50.0, hold_wns=-140.0, hold_tns=-500.0), -20.0, -193.0, False, "2 x"),
    (dict(setup_wns=-50.0, hold_wns=-120.0, hold_tns=-500.0), -20.0, -193.0, True, "loss 30.00"),
    # past the budget even with a huge gain
    (dict(setup_wns=-65.0, hold_wns=5.0), -20.0, -193.0, False, "allowance 40.00"),
    # closed hold, small loss in noise band: always OK
    (dict(setup_wns=-61.5, hold_wns=6.2), -60.07, -4.55, True, "CLOSED"),
    # closed hold, loss 9.9 > floor, gain 10.75 < 2 x 9.9 (test_post_route_eco case)
    (dict(setup_wns=-70.0, hold_wns=6.2), -60.07, -4.55, False, "2 x"),
    # hold TNS worse
    (dict(R210551, hold_tns=-40000.0), -139.941, -193.439, False, "hold TNS"),
    # violators rose
    (dict(R210551, pre_hold_viol=677, hold_viol=700), -139.941, -193.439, False, "violators"),
    # setup TNS > 25 % worse
    (dict(R210551, setup_tns=-3300.0), -139.941, -193.439, False, "setup TNS"),
    # hold was already closed / no data / hold got worse
    (dict(R210551), -139.941, 2.0, False, "n/a"),
    (dict(R210551, ok=False), -139.941, -193.439, False, "n/a"),
    (dict(R210551, hold_wns=-200.0), -139.941, -193.439, False, "did not improve"),
])
def test_tradeoff(kw, b_setup, b_hold, ok, frag):
    got, why = A._eco_hold_tradeoff(_eco(**kw), b_setup, b_hold, KN)
    assert got is ok, why
    assert frag in why


def test_min_gain_zero_disables_partial_but_not_closed():
    kn = dict(KN, JANUS_ECO_HOLD_MIN_GAIN_PS=0.0)
    assert not A._eco_hold_tradeoff(_eco(**R210551), -139.941, -193.439, kn)[0]
    assert A._eco_hold_tradeoff(_eco(setup_wns=-61.5, hold_wns=6.2), -60.07, -4.55, kn)[0]


def test_parse_pre_tns_and_hold_viol():
    out = ("JANUS_PR_PRE setup wns -139.941\nJANUS_PR_PRE setup tns -2513.5\n"
           "JANUS_PR_PRE hold wns -193.44\nJANUS_PR_PRE hold tns -31427.2\n"
           "JANUS_PR_PRE hold viol 677\nJANUS_PR drc 0\nJANUS_PR setup wns -142.6\n"
           "JANUS_PR hold wns -120.7\nJANUS_PR hold tns -646\nJANUS_PR hold viol 41\n"
           "JANUS_PR wrote x.odb\n")
    r = parse_eco(out, mode="hold")
    assert r.ok and r.pre_setup_tns == -2513.5 and r.pre_hold_tns == -31427.2
    assert (r.pre_hold_viol, r.hold_viol) == (677, 41)
    assert r.hold_tns == -646.0                 # viol line not mistaken for tns


def test_tcl_reports_hold_viol():
    s = open(os.path.join(REPO, "asic_flow", "scripts", "post_route_opt.tcl")).read()
    body = s[s.index("proc _report_slacks"):s.index("proc _slew_viol")]
    assert "sta::endpoint_violation_count min" in body and 'hold viol $n' in body


def test_knobs_read_from_mk_and_pinned_in_soc_top(tmp_path):
    mk = open(os.path.join(REPO, "asic_flow", "configs", "janus_soc_top.mk")).read()
    for k, v in A._ECO_TRADEOFF_DEFAULTS.items():
        m = re.search(rf"(?m)^export {k}\s*=\s*(\S+)", mk)
        assert m and float(m.group(1)) == v, k
    a = A.__new__(A)
    a.design_root = str(tmp_path)
    (tmp_path / "janus_soc_top").mkdir()
    (tmp_path / "janus_soc_top" / "config.mk").write_text(
        "export JANUS_ECO_HOLD_EXCHANGE_K = 3  # c\noverride JANUS_ECO_SETUP_MARGIN_PS := 60\n"
        "JANUS_ECO_HOLD_SPARE_PS ?= 8\nexport JANUS_ECO_SETUP_LOSS_FRAC\n")
    kn = a._eco_tradeoff_knobs("janus_soc_top")
    assert kn["JANUS_ECO_HOLD_EXCHANGE_K"] == 3.0
    assert kn["JANUS_ECO_SETUP_MARGIN_PS"] == 60.0
    assert kn["JANUS_ECO_HOLD_SPARE_PS"] == 8.0
    assert kn["JANUS_ECO_SETUP_LOSS_FRAC"] == 0.5          # bare export: default
    assert kn["JANUS_ECO_HOLD_MIN_GAIN_PS"] == 20.0


def test_user_core_eco_margin_export_now_seen(tmp_path):
    a = A.__new__(A)
    a.design_root = str(tmp_path)
    (tmp_path / "user_core").mkdir()
    (tmp_path / "user_core" / "config.mk").write_text("export JANUS_ECO_HOLD_MARGIN_PS   = 20\n")
    assert a._eco_hold_margin("user_core") == "20"


# ---- end-to-end through _eco_pass_orfs / _post_route_opt (mocked make) ----
TXT = """JANUS_PR_BEGIN hold
JANUS_PR_PRE setup wns -139.941
JANUS_PR_PRE setup tns -2513.503
JANUS_PR_PRE hold wns -193.439
JANUS_PR_PRE hold tns -31427.188
JANUS_PR drc 0
JANUS_PR setup wns -142.621
JANUS_PR setup tns -2475.835
JANUS_PR hold wns -120.734
JANUS_PR hold tns -645.911
JANUS_PR wrote {odb}
"""


def test_run_210551_adopted_by_tradeoff(tmp_path, monkeypatch):
    from test_post_route_eco import _eco_agent, _patch_popen, _patch_gds
    a, res = _eco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, TXT)
    _patch_gds(monkeypatch, res)
    assert a._eco_pass_orfs("user_core_ara", "hold", -139.94, -193.44) == (-142.621, -120.734)
    log = open(a._block_log("user_core_ara")).read()
    assert "hold ECO ADOPTED" in log and "allowance 5.00" in log


def test_reject_logs_tradeoff_reason(tmp_path, monkeypatch):
    from test_post_route_eco import _eco_agent, _patch_popen, _patch_gds
    a, res = _eco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, TXT.replace("hold tns -645.911", "hold tns -40000"))
    _patch_gds(monkeypatch, res)
    assert a._eco_pass_orfs("user_core_ara", "hold", -139.94, -193.44) == (-139.94, -193.44)
    log = open(a._block_log("user_core_ara")).read()
    assert "hold ECO DISCARDED" in log and "§232 trade-off: hold TNS worse" in log


def test_setup_pass_must_improve_setup(tmp_path, monkeypatch):
    from test_post_route_eco import _eco_agent, _patch_popen, _patch_gds
    a, res = _eco_agent(tmp_path)
    txt = ("JANUS_PR_PRE setup wns -60.0\nJANUS_PR_PRE hold wns 8.0\nJANUS_PR drc 0\n"
           "JANUS_PR setup wns -60.0\nJANUS_PR hold wns 9.0\nJANUS_PR wrote {odb}\n")
    _patch_popen(monkeypatch, a, res, txt)
    _patch_gds(monkeypatch, res)
    assert a._eco_pass_orfs("user_core_ara", "setup", -60.0, 8.0) == (-60.0, 8.0)


@pytest.mark.parametrize("hold_after,want", [
    (2.0, ["hold"]), (5.0, ["hold", "setup"]), (12.0, ["hold", "setup"])])
def test_setup_recovery_needs_hold_spare(tmp_path, hold_after, want):
    from test_post_route_eco import _eco_agent
    a, res = _eco_agent(tmp_path)
    a.results["user_core_ara"].wns_ns, a.results["user_core_ara"].hold_ps = -40.0, -30.0
    a._find_routed_odb = lambda b: str(res / "6_final.odb")
    a._orfs_lib_env = lambda b: {}
    a.orfs = None
    calls = []

    def fake_pass(block, mode, s, h):
        calls.append(mode)
        return (s, hold_after) if mode == "hold" else (s + 10, h)
    a._eco_pass_orfs = fake_pass
    a._post_route_opt("user_core_ara")
    assert calls == want
    if want == ["hold"]:
        assert "hold spare < 5" in open(a._block_log("user_core_ara")).read()
