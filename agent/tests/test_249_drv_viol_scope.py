"""§249: post-route max-slew ECO scope `viol` (RCX violators only, setup
guard, calibrated margin), decide_drv on a slightly hold-negative block,
per-block setup tolerance, and --eco-drv-only email/Drive notify on every exit."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from incremental_opt import ECOResult, decide_drv  # noqa: E402
from test_183_port_slew_eco import (_closed_agent, _eco_agent, _out,  # noqa: E402
                                    _patch_popen, BLK)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _r(setup=-68.0, hold=-7.0, drc=0, pre=277, post=40, hv_pre=2, hv=2):
    return ECOResult(ok=True, mode="drv", setup_wns=setup, hold_wns=hold, drc=drc,
                     pre_slew_viol=pre, slew_viol=post, wrote=True,
                     pre_hold_viol=hv_pre, hold_viol=hv)


# ---------------------------------------------------------------- decide_drv --
@pytest.mark.parametrize("kw,verdict,frag", [
    ({}, "adopt", "277 -> 40"),                       # soc_top: hold -7.31 kept
    ({"hold": -8.5}, "discard", "hold regressed"),    # worse than noise
    ({"hv": 3}, "discard", "hold violators 2 -> 3"),
    ({"setup": -69.8}, "discard", "setup regressed"),  # tol 1 ps
    ({"setup": -69.5}, "adopt", "277 -> 40"),          # within 1 ps
])
def test_decide_drv_soc_top(kw, verdict, frag):
    v, why = decide_drv(-68.67, -7.31, 0, _r(**kw), setup_tol_ps=1.0)
    assert v == verdict and frag in why, why


def test_decide_drv_closed_hold_still_must_stay_non_negative():
    v, why = decide_drv(67.27, 17.4, 0, _r(setup=66.0, hold=-0.5, hv_pre=0, hv=1))
    assert v == "discard" and "< 0" in why


# ------------------------------------------------------------- agent wiring --
def test_viol_scope_and_knobs_reach_make(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    knobs = {"JANUS_ECO_DRV_SCOPE": "viol", "JANUS_ECO_DRV_SETUP_GUARD_PS": "100",
             "JANUS_ECO_DRV_MAX_MARGIN": "60"}
    a._janus_knob = lambda b, n: knobs.get(n)
    seen = []
    _patch_popen(monkeypatch, a, res, _out(), seen=seen)
    a._run_eco_orfs(BLK, "drv")
    cmd = seen[0]
    assert "JANUS_PR_DRV_SCOPE=viol" in cmd
    assert "JANUS_PR_DRV_SETUP_GUARD_PS=100" in cmd and "JANUS_PR_DRV_MAX_MARGIN=60" in cmd
    knobs["JANUS_ECO_DRV_SETUP_GUARD_PS"] = "junk"
    assert a._eco_drv_float_knob(BLK, "JANUS_ECO_DRV_SETUP_GUARD_PS", 50.0) == 50.0


def test_drv_pass_uses_block_setup_tol(tmp_path, monkeypatch):
    a, res = _closed_agent(tmp_path)
    a._janus_knob = lambda b, n: {"JANUS_ECO_DRV_SETUP_TOL_PS": "0.5"}.get(n)
    seen = {}
    import janus_agent as ja

    def _dd(bs, bh, bd, eco, setup_tol_ps=5.0):
        seen["tol"] = setup_tol_ps
        return "discard", "x"
    monkeypatch.setattr(ja, "decide_drv", _dd)
    _patch_popen(monkeypatch, a, res, _out())
    assert a._eco_drv_pass(BLK) is False and seen["tol"] == 0.5


# ------------------------------------------------------- --eco-drv-only mail --
def _wire(a, res, calls, adopt):
    a._find_routed_odb = lambda b: str(res / "6_final.odb")
    a._classify_timing = lambda b: None
    a._eco_adopted_timing = lambda b: {"setup_ws": -68.67, "hold_ws": -7.31, "source": "eco"}
    a._eco_drv_pass = lambda b: calls.append("drv") or adopt
    a._run_abstract = lambda b: calls.append("abs")
    a._run_signoff = lambda b: calls.append("so") or "ok"
    a._archive_golden = lambda b, r=None: calls.append("gold")
    a._notify_settled = lambda b, r: calls.append(("mail", r.status, r.note.split("; ")[-1]))


def test_eco_drv_only_adopt_notifies(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.blocks = [BLK]
    calls = []
    _wire(a, res, calls, adopt=True)
    assert a._eco_drv_only() == 0
    assert calls == ["drv", "abs", "so", "gold",
                     ("mail", "DONE", "eco-drv-only exit 0: drv ECO ADOPTED")]
    assert a.results[BLK].hold_ps == -7.31          # baseline = in-force ECO RCX


def test_eco_drv_only_discard_notifies_exit5(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.blocks = [BLK]
    calls = []
    _wire(a, res, calls, adopt=False)
    assert a._eco_drv_only() == 5
    assert calls == ["drv", ("mail", "DONE",
                             "eco-drv-only exit 5: drv ECO DISCARDED, previous 6_final kept")]


def test_eco_drv_only_no_odb_notifies_exit3(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.blocks = [BLK]
    a._find_routed_odb = lambda b: None
    mails = []
    a._notify_settled = lambda b, r: mails.append((r.status, r.note))
    assert a._eco_drv_only() == 3
    assert mails and mails[0][0] == "FAILED" and "exit 3" in mails[0][1]


# ---------------------------------------------------------------- config/tcl --
def test_soc_top_drv_knobs():
    with open(os.path.join(_ROOT, "asic_flow", "configs", "janus_soc_top.mk")) as fh:
        mk = fh.read()

    def k(n):
        m = re.search(rf"^export\s+{n}\s*=\s*(\S+)", mk, re.M)
        return m and m.group(1)
    assert k("JANUS_ECO_DRV_SCOPE") == "viol"
    assert float(k("JANUS_ECO_DRV_SETUP_TOL_PS")) == 1.0
    assert float(k("JANUS_ECO_DRV_SETUP_GUARD_PS")) == 100.0


def test_tcl_viol_scope_collects_on_rcx_before_estimate():
    with open(os.path.join(_ROOT, "asic_flow", "scripts", "post_route_opt.tcl")) as fh:
        t = fh.read()
    drv = t[t.index('} elseif {$mode eq "drv"}'):t.index('} elseif {$mode eq "drc"}')]
    assert drv.index("janus_drv_rcx_violators") < drv.index("estimate_parasitics -placement")
    assert drv.index("estimate_parasitics -placement") < drv.index("janus_drv_calibrated_margin")
    assert "-slew_margin $slew_margin" in drv
    assert "proc janus_drv_rcx_violators" in t and "proc janus_drv_calibrated_margin" in t
