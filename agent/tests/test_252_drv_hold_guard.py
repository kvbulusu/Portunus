"""§252: hold-safe drv ECO -- RCX hold guard on the viol scope and a
dont-touch/FIRM lock on hold-critical cells during repair_design + DPL."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_183_port_slew_eco import _eco_agent, _out, _patch_popen, BLK  # noqa: E402

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _tcl():
    with open(os.path.join(_ROOT, "asic_flow", "scripts", "post_route_opt.tcl")) as fh:
        return fh.read()


def test_hold_guard_reaches_make(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    knobs = {"JANUS_ECO_DRV_SCOPE": "viol", "JANUS_ECO_DRV_HOLD_GUARD_PS": "75"}
    a._janus_knob = lambda b, n: knobs.get(n)
    seen = []
    _patch_popen(monkeypatch, a, res, _out(), seen=seen)
    a._run_eco_orfs(BLK, "drv")
    assert "JANUS_PR_DRV_HOLD_GUARD_PS=75" in seen[0]


def test_hold_guard_default_off(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    a._janus_knob = lambda b, n: None
    seen = []
    _patch_popen(monkeypatch, a, res, _out(), seen=seen)
    a._run_eco_orfs(BLK, "drv")
    assert "JANUS_PR_DRV_HOLD_GUARD_PS=0" in seen[0]


def test_soc_top_hold_guard_knob():
    with open(os.path.join(_ROOT, "asic_flow", "configs", "janus_soc_top.mk")) as fh:
        m = re.search(r"^export\s+JANUS_ECO_DRV_HOLD_GUARD_PS\s*=\s*(\S+)", fh.read(), re.M)
    # must exceed the 47 ps hold shift seen in 20261117_121711
    assert m and float(m.group(1)) > 47.0


def test_tcl_hold_guard_and_lock_order():
    t = _tcl()
    for p in ("proc janus_net_hold_slack", "proc janus_drv_hold_critical_insts",
              "proc janus_drv_lock_insts", "proc janus_drv_unlock_insts"):
        assert p in t
    drv = t[t.index('} elseif {$mode eq "drv"}'):t.index('} elseif {$mode eq "drc"}')]
    # hold-critical cells collected on RCX, before the estimate switch
    assert drv.index("janus_drv_hold_critical_insts") < drv.index("estimate_parasitics -placement")
    assert "janus_drv_rcx_violators $guard $hguard" in drv
    # locked before repair_design, unlocked after reroute and before write_db
    assert drv.index("janus_drv_lock_insts") < drv.index("catch {repair_design")
    assert drv.index("    _reroute") < drv.index("janus_drv_unlock_insts") < drv.index("_write_outputs")
    lock = t[t.index("proc janus_drv_lock_insts"):t.index("proc janus_drv_unlock_insts")]
    assert "setDoNotTouch 1" in lock and "setPlacementStatus FIRM" in lock
    assert "isBlock" in lock and 'ne "PLACED"' in lock and "vnets" in lock
