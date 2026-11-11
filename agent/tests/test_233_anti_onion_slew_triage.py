"""§233 anti-onion (run 20261109_210551): const-fold every constant net on
janus_soc_top and the janus_dbg unit 6b max-slew triage."""
import os
import re
import shutil
import subprocess
import sys

import pytest

FLOW = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(FLOW, "utils"))
sys.path.insert(0, os.path.join(FLOW, "agent"))

import flow_state  # noqa: E402
HOOKS = os.path.join(FLOW, "scripts", "hooks")
DBG = os.path.join(HOOKS, "janus_debug_reports.tcl")


def _read(*p):
    return open(os.path.join(*p)).read()


def test_soc_top_folds_all_constant_nets():
    txt = _read(FLOW, "configs", "janus_soc_top.mk")
    assert re.search(r"^export JANUS_CONST_FOLD_MIN_FANOUT = 1$", txt, re.M)
    assert "export PRE_RESIZE_TCL         = $(JANUS_CONST_FOLD_TCL)" in txt
    # resume point: knob invalidates from 3_place (restamp needed)
    assert flow_state._earliest_stage_for_keys(["JANUS_CONST_FOLD_MIN_FANOUT"]) == "3_place"


def test_const_fold_min_fanout_1_keeps_single_load_nets():
    # with fo=1 the iterm guard (<= fo) still admits driver + 1 load
    txt = _read(HOOKS, "janus_const_fold.tcl")
    assert "if {[llength $its] <= $fo} continue" in txt
    assert "[llength $loads] < $fo" in txt


def test_debug_reports_has_slew_triage_unit():
    txt = _read(DBG)
    assert "proc slew_triage { topn }" in txt
    assert 'unit "6b DRV triage' in txt
    assert txt.index('unit "6b DRV triage') < txt.index('unit "7 power')


MOCK = r'''
source {DBG}
proc report_check_types {args} { return "max slew\n\nPin Limit Slew Slack\n----\nu_clkgen/pll_locked_o\$_DFFE_PN0P_/SETN  320.00 3372.64 -3052.64 (VIOLATED)\nu_p/wacc_q\[1\]/SETN  320.00  440.79 -120.79 (VIOLATED)\n" }
namespace eval ord { proc get_db_block {} { return BLK } }
proc BLK {m args} { switch $m { getDbUnitsPerMicron {return 1000} findITerm { return IT_[string length [lindex $args 0]] } } }
foreach n {IT_38 IT_17} { proc $n {m args} { switch $m { getNet {return NET} } } }
proc NET {m args} { switch $m { getSigType {return SIGNAL} isDoNotTouch {return 0} getITerms {return {LD DRV}} getBTerms {return {}} } }
proc LD {m args} { switch $m { getAvgXY {return {1 0 0}} isOutputSignal {return 0} } }
proc DRV {m args} { switch $m { getAvgXY {return {1 400000 20000}} isOutputSignal {return 1} getInst {return INST} getMTerm {return MT} } }
proc INST {m args} { return "u_x/_1_" }
proc MT {m args} { return "Y" }
proc get_pins {p} { return P }
namespace eval sta { proc pin_sim_logic_value {p} { return 1 } }
set ::janus_dbg::fid stdout
::janus_dbg::slew_triage 20
'''


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_slew_triage_mock_run(tmp_path):
    f = tmp_path / "m.tcl"
    f.write_text(MOCK.replace("{DBG}", "{" + DBG + "}"))
    out = subprocess.run(["tclsh", str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    s = out.stdout
    assert "pll_locked_o" in s and "SETN" in s
    assert "u_x/_1_/Y" in s or "u_x/_1_" in s
    assert "SIGNAL" in s
