"""§161: detour leg repeaters, constant-net fold, slew margin, macro-pin skew."""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FLOW = os.path.join(HERE, "..", "..")
sys.path.insert(0, os.path.join(FLOW, "utils"))
sys.path.insert(0, os.path.join(FLOW, "agent"))

import adaptive_closure as ac  # noqa: E402
import flow_state  # noqa: E402

HOOKS = os.path.join(FLOW, "scripts", "hooks")


def _read(*p):
    return open(os.path.join(*p)).read()


def test_new_keys_resume_from_place_not_synth():
    for k in ("SLEW_MARGIN", "CAP_MARGIN", "PRE_RESIZE_TCL", "JANUS_CONST_FOLD_TCL",
              "JANUS_CONST_FOLD_MIN_FANOUT"):
        assert flow_state._earliest_stage_for_keys([k]) == "3_place", k
    assert flow_state._earliest_stage_for_keys(["JANUS_MACRO_DETOUR_SEG_UM"]) == "5_route"


def test_user_core_mk_wires_fold_and_margins():
    txt = _read(FLOW, "configs", "user_core.mk")
    assert "export PRE_RESIZE_TCL = $(JANUS_CONST_FOLD_TCL)" in txt
    assert re.search(r"^export JANUS_CONST_FOLD_TCL = .*/hooks/janus_const_fold\.tcl$", txt, re.M)
    assert re.search(r"^export SLEW_MARGIN\s+= 20$", txt, re.M)
    assert re.search(r"^export CAP_MARGIN\s+= 20$", txt, re.M)


def test_adaptive_pre_chains_const_fold():
    assert "source $::env(JANUS_CONST_FOLD_TCL)" in _read(HOOKS, "janus_adaptive_pre.tcl")


def test_const_fold_hook_guards():
    h = _read(HOOKS, "janus_const_fold.tcl")
    assert "sta::pin_sim_logic_value" in h
    assert "set_case_analysis" in h          # refuses when sim constants are SDC-made
    assert "isDoNotTouch" in h and "isBlock" in h
    assert "JANUS_CFOLD: failed while rewiring" in h


def test_detour_hook_inserts_dont_touch_leg_repeaters():
    h = _read(HOOKS, "janus_macro_detour.tcl")
    assert "JANUS_MACRO_DETOUR_SEG_UM 100" in h
    assert '"janus_detour_${k}_r${i}"' in h
    assert "foreach l $legs { $l setDoNotTouch 1 }" in h
    # the far-side net stays repairable
    assert "$nB setDoNotTouch" not in h


def test_tcl_syntax():
    if not shutil.which("tclsh"):
        return
    for f in ("janus_const_fold.tcl", "janus_macro_detour.tcl", "janus_adaptive_pre.tcl"):
        r = subprocess.run(["tclsh"], input=f"set s [info complete [read [open {{{os.path.join(HOOKS, f)}}}]]]; puts $s",
                           capture_output=True, text=True)
        assert r.stdout.strip() == "1", f


QOR_195409 = {
    "clock": {"name": "core_clk", "period_ps": 2857.14},
    "timing": {"setup_worst_slack": -247.30, "hold_worst_slack": 15.11},
    "clock_tree": {"setup_skew": 1338.49, "source_latency": 1719.65, "target_latency": -458.39,
                   "source_latency_pin": "i_ara_system.i_system_mux.a_full_q$_DFFE_PN0P_/CLK ^",
                   "target_latency_pin": "i_ara_system.i_ara/clk_i ^"},
    "drv": {"max_slew": 398, "max_cap": 0},
    "grt_overflow": {"total": 0},
    "area": {"utilization_pct": 75.9},
}


def test_macro_pin_skew_is_not_clock_skew_limited():
    fq = ac.from_qor(QOR_195409)
    assert "skew_ps" not in fq and "i_ara_system.i_ara/clk_i" in fq["skew_untrusted"]
    r = ac.classify(fq)
    assert r["class"] != "CLOCK_SKEW_LIMITED"
    assert r["class"] != "CONGESTION_LIMITED"   # 0 GRT overflow: util alone is not congestion
    assert any("ignored" in e for e in r["evidence"])


def test_flop_skew_still_classified():
    # agent_domain fixture shape: both ends are flops (target latency prints negative)
    q = dict(QOR_195409, clock_tree={"setup_skew": 600.0, "source_latency": 900.0,
                                     "target_latency": -300.0,
                                     "source_latency_pin": "u/a_q$_DFF_P_/CLK ^",
                                     "target_latency_pin": "u/b_q$_DFF_P_/CLK ^"})
    fq = ac.from_qor(q)
    assert fq["skew_ps"] == 600.0
    assert ac.classify(fq)["class"] == "CLOCK_SKEW_LIMITED"


def test_util_hot_without_overflow_info_still_congestion():
    q = {"period_ps": 2857.0, "groups": {"reg2reg": {"wns_ps": -100}}, "util": 0.8}
    assert ac.classify(q)["class"] == "CONGESTION_LIMITED"
