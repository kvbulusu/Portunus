"""§253: a drv ECO that passes every gate except hold gets ONE chained hold
pass on its own (pre-RCX) output; the combined result is judged by decide_drv
against the pre-drv baseline. Opt-in knob JANUS_ECO_DRV_CHAIN_HOLD."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from incremental_opt import ECOResult  # noqa: E402
from test_183_port_slew_eco import _closed_agent, BLK  # noqa: E402

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_KN = {"JANUS_ECO_DRV_CHAIN_HOLD": "1", "JANUS_ECO_DRV_SETUP_TOL_PS": "1",
       "JANUS_ECO_HOLD_ONLY_STRATEGY": "pin"}


def _drv(hold=-13.969, setup=-59.37, drc=0, post=9):
    # soc_top 20261117_230708 drv numbers
    return ECOResult(ok=True, mode="drv", setup_wns=setup, hold_wns=hold, drc=drc,
                     pre_slew_viol=253, slew_viol=post, wrote=True, pre_setup=-63.73,
                     pre_hold=5.64, pre_hold_viol=0, hold_viol=1)


def _hold(hold=12.0, setup=-59.4, drc=0, hv=0, slew=9):
    return ECOResult(ok=True, mode="hold", setup_wns=setup, hold_wns=hold, drc=drc,
                     pre_slew_viol=9, slew_viol=slew, wrote=True, pre_setup=-59.37,
                     pre_hold=-13.969, pre_hold_viol=1, hold_viol=hv)


def _wire(tmp_path, knobs, drv, hold):
    a, res = _closed_agent(tmp_path)
    a._janus_knob = lambda b, n: knobs.get(n)
    calls, adopted, hist = [], [], []

    def _run(block, mode, in_odb="6_final.odb", in_spef=None, tag_mode=None, extra=None):
        tm = tag_mode or mode
        outs = {k: str(res / f"janus_eco_{tm}.{k}") for k in ("odb", "spef", "def", "v")}
        for p in outs.values():
            open(p, "w").write(tm)
        calls.append((mode, in_odb, in_spef, tm, list(extra or [])))
        return (drv if mode == "drv" else hold), outs
    a._run_eco_orfs = _run
    a._adopt_eco_orfs = lambda b, outs, bak=".preeco": adopted.append(outs["odb"]) or True
    a._eco_record_adopted = lambda b, m, e: None
    a._eco_history_append = lambda b, m, v, w, e, t0=None: hist.append((m, v, w))
    return a, res, calls, adopted, hist


def test_chain_adopts_when_hold_pass_closes(tmp_path):
    a, res, calls, adopted, hist = _wire(tmp_path, _KN, _drv(), _hold())
    assert a._eco_drv_pass(BLK) is True
    assert [c[0] for c in calls] == ["drv", "hold"]
    mode, in_odb, in_spef, tm, extra = calls[1]
    assert in_odb == "janus_eco_drv.odb" and in_spef.endswith("janus_eco_drv.spef")
    assert tm == "drv_hold"
    assert "JANUS_PR_HOLD_STRATEGY=pin" in extra and "JANUS_ECO_HOLD_PIN_SELECT_PS=10" in extra
    assert adopted == [str(res / "janus_eco_drv_hold.odb")]
    assert not os.path.exists(res / "janus_eco_drv.odb")       # intermediate dropped
    assert "+eco(drv_hold) slew 253->9" in a.results[BLK].note
    assert a.results[BLK].hold_ps == 12.0
    assert hist[0][:2] == ("drv", "DISCARDED") and "§253" in hist[0][2]
    assert hist[-1][:2] == ("drv_hold", "ADOPTED") and "drv+hold chain" in hist[-1][2]


def test_chain_judged_vs_pre_drv_baseline(tmp_path):
    # hold pass leaves 4.0 < 5.64 - 1 ps noise -> discard, both outputs dropped
    a, res, calls, adopted, hist = _wire(tmp_path, _KN, _drv(), _hold(hold=4.0))
    assert a._eco_drv_pass(BLK) is False and adopted == []
    assert hist[-1][:2] == ("drv_hold", "DISCARDED") and "hold regressed" in hist[-1][2]
    assert not os.path.exists(res / "janus_eco_drv_hold.odb")


def test_chain_setup_tol_and_slew_vs_original(tmp_path):
    a, *_r, adopted, hist = _wire(tmp_path, _KN, _drv(), _hold(setup=-64.9))
    assert a._eco_drv_pass(BLK) is False and "setup regressed" in hist[-1][2]
    a, *_r, adopted, hist = _wire(tmp_path / "b", _KN, _drv(), _hold(slew=253))
    assert a._eco_drv_pass(BLK) is False and "max-slew not reduced" in hist[-1][2]


def test_no_chain_when_knob_off_or_other_gate_fails(tmp_path):
    kn = dict(_KN, JANUS_ECO_DRV_CHAIN_HOLD="0")
    a, _res, calls, *_ = _wire(tmp_path, kn, _drv(), _hold())
    assert a._eco_drv_pass(BLK) is False and [c[0] for c in calls] == ["drv"]
    for i, d in enumerate([_drv(drc=3), _drv(post=253), _drv(setup=-65.0)]):
        a, _res, calls, *_ = _wire(tmp_path / str(i), _KN, d, _hold())
        assert a._eco_drv_pass(BLK) is False and [c[0] for c in calls] == ["drv"]


def test_repair_strategy_has_no_pin_extras(tmp_path):
    kn = dict(_KN, JANUS_ECO_HOLD_ONLY_STRATEGY="repair")
    a, _res, calls, *_ = _wire(tmp_path, kn, _drv(), _hold())
    a._eco_drv_pass(BLK)
    assert calls[1][4] == []


def test_soc_top_knobs():
    mk = open(os.path.join(_ROOT, "asic_flow", "configs", "janus_soc_top.mk")).read()
    assert re.search(r"^export JANUS_ECO_DRV_CHAIN_HOLD\s*=\s*1\s*$", mk, re.M)
    assert re.search(r"^export JANUS_ECO_DRV_CHAIN_HOLD_SELECT_PS\s*=\s*10\s*$", mk, re.M)
    others = [f for f in os.listdir(os.path.join(_ROOT, "asic_flow", "configs"))
              if f.endswith(".mk") and f != "janus_soc_top.mk"]
    for f in others:
        assert "JANUS_ECO_DRV_CHAIN_HOLD" not in open(
            os.path.join(_ROOT, "asic_flow", "configs", f)).read(), f
