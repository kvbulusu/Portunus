"""§169 user_domain CTS: -no_insertion_delay knob, macro pins out of the IO median, 2-flop clock_en."""
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "utils"))
import flow_state as fs  # noqa: E402

ASIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ROOT = os.path.dirname(ASIC)
CFG = os.path.join(ASIC, "configs")
CTS_HOOK = os.path.join(ASIC, "scripts", "hooks", "janus_cts_balance.tcl")
RTL = os.path.join(ROOT, "rtl", "user", "user_cluster_top.sv")


def _read(p):
    with open(p) as f:
        return f.read()


def test_mk_knobs():
    mk = _read(os.path.join(CFG, "user_domain.mk"))
    assert re.search(r"^export\s+JANUS_CTS_NO_INSERTION_DELAY\s+=\s*1\s*$", mk, re.M)
    assert re.search(r"^JANUS_UD_CLKEN_PIPE \?= 1$", mk, re.M)
    assert "ifeq ($(JANUS_UD_BOUNDARY_CUT)$(JANUS_UD_CLKEN_PIPE),11)" in mk
    assert "export VERILOG_DEFINES += -D JANUS_UD_CLKEN_PIPE" in mk
    # §168 knobs still in place
    assert re.search(r"^export\s+JANUS_CTS_IO_LATENCY_TRACK\s+=\s*1", mk, re.M)


def test_other_blocks_do_not_set_no_insertion_delay():
    for name in os.listdir(CFG):
        if name.endswith(".mk") and name != "user_domain.mk":
            assert "JANUS_CTS_NO_INSERTION_DELAY" not in _read(os.path.join(CFG, name)), name


def test_resume_stage_mapping():
    assert fs._earliest_stage_for_keys(["JANUS_CTS_NO_INSERTION_DELAY"]) == "4_cts"
    assert fs._earliest_stage_for_keys(["VERILOG_DEFINES"]) == "1_synth"


def test_rtl_two_stage_clock_en():
    sv = _read(RTL)
    blk = sv[sv.index("`ifdef JANUS_UD_CLKEN_PIPE"):sv.index("assign core_clk_en =")]
    assert "core_clk_en_q1 <= {NUM_CORES{pd_perf_en_i & fetch_enable_i}} & core_clock_en_i;" in blk
    assert "core_clk_en    <= core_clk_en_q1;" in blk
    # single-flop path kept for CLKEN_PIPE=0
    assert blk.count("core_clk_en <= {NUM_CORES{pd_perf_en_i & fetch_enable_i}} & core_clock_en_i;") == 1
    assert ".clock_en_i   (core_clk_en[gc])" in sv


def test_hook_wrapper_and_macro_exclusion_text():
    t = _read(CTS_HOOK)
    assert "proc janus_cts_is_macro_pin" in t and "isBlock" in t
    assert "-no_insertion_delay" in t
    assert "JANUS_CTS_NO_INS: armed" in t
    assert "macro clock pins (excluded)" in t
    for kw in ("set_false_path", "set_multicycle_path", "set_max_delay", "set_disable_timing"):
        assert kw not in t


MOCK = r"""
proc get_clocks {args} { return "" }
proc all_registers {args} { return {} }
proc estimate_parasitics {args} {}
set ::calls {}
proc clock_tree_synthesis {args} {
  lappend ::calls $args
  if { $::env(MOCK_REJECT) && [lsearch $args -no_insertion_delay] >= 0 } { error "unknown key -no_insertion_delay" }
  return 0
}
source {HOOK}
clock_tree_synthesis -sink_clustering_enable -repair_clock_nets
puts "CALLS=[join $::calls |]"
"""


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not available")
@pytest.mark.parametrize("reject,noins", [(0, "1"), (1, "1"), (0, "0")])
def test_wrapper_args_and_fallback(tmp_path, reject, noins):
    f = tmp_path / "m.tcl"
    f.write_text(MOCK.replace("{HOOK}", CTS_HOOK))
    env = dict(os.environ, MOCK_REJECT=str(reject), JANUS_CTS_NO_INSERTION_DELAY=noins,
               JANUS_CTS_IO_LATENCY_TRACK="1")
    out = subprocess.run(["tclsh", str(f)], capture_output=True, text=True, env=env, timeout=60).stdout
    calls = re.search(r"CALLS=(.*)", out).group(1).split("|")
    base = "-sink_clustering_enable -repair_clock_nets"
    if noins == "0":
        assert calls == [base]
        assert "JANUS_CTS_NO_INS" not in out
    elif reject:
        assert calls == [base + " -no_insertion_delay", base]
        assert "flag not supported" in out
    else:
        assert calls == [base + " -no_insertion_delay"]
