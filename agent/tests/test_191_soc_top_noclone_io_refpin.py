"""§191 janus_soc_top: no gate cloning of the clkgen roots + multi-clock IO -reference_pin.

Run 20261101_165732: repair_timing cloned u_clkgen/_146_/_147_ (clk_user leaked
into the agent tree via the clones), and the top IO used ideal clock edges vs a
~4 ns propagated register tree (in2reg hold -2823 -> 9580 hold buffers, reg2out
setup -2282).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"
SDC = ROOT / "asic_flow/configs/constraints/janus_soc_top.sdc"
HOOK = ROOT / "asic_flow/scripts/hooks/janus_cts_balance.tcl"


def _mkval(key):
    m = re.search(rf"^export\s+{re.escape(key)}\s*=\s*(.*)$", MK.read_text(), re.M)
    assert m, key
    return m.group(1).strip()


def _sdc_list(var):
    m = re.search(rf"^set\s+{var}\s+\[janus_ports\s+\{{([^}}]*)\}}\]", SDC.read_text(), re.M)
    assert m, var
    return m.group(1).split()


def test_skip_gate_cloning_191():
    assert _mkval("SKIP_GATE_CLONING") == "1"
    # unconditional (outside the JANUS_FLAT block)
    txt = MK.read_text()
    assert txt.index("export SKIP_GATE_CLONING") > txt.index("export JANUS_CTS_IO_REFPIN_OUT_OVERRIDE")
    import sys
    sys.path.insert(0, str(ROOT / "asic_flow/utils"))
    import flow_state as fs
    assert fs._earliest_stage_for_keys(["SKIP_GATE_CLONING"]) == "2_floorplan"
    assert fs._earliest_stage_for_keys(["JANUS_CTS_IO_REFPIN_MULTI"]) == "4_cts"


def test_mk_port_lists_mirror_sdc_191():
    assert _mkval("JANUS_CTS_IO_REFPIN_MULTI") == "1"
    assert _mkval("JANUS_CTS_IO_REFPIN_CLOCKS").split() == ["clk_user", "clk_agent"]
    assert float(_mkval("JANUS_CTS_IO_REFPIN_FRAC")) == pytest.approx(0.30)
    assert "0.30 * 2857.14" in SDC.read_text() and "0.30 * 5000.0" in SDC.read_text()
    for mkk, sdcv in (("JANUS_CTS_IO_REFPIN_IN_clk_user", "_in_user"),
                      ("JANUS_CTS_IO_REFPIN_IN_clk_agent", "_in_agent"),
                      ("JANUS_CTS_IO_REFPIN_OUT_clk_user", "_out_user"),
                      ("JANUS_CTS_IO_REFPIN_OUT_clk_agent", "_out_agent")):
        assert _mkval(mkk).split() == _sdc_list(sdcv), mkk
    assert _sdc_list("_ara_busy") == ["ara_busy_o"]
    assert re.search(r"set_output_delay -clock clk_user 0\.0 \$_ara_busy", SDC.read_text())
    assert _mkval("JANUS_CTS_IO_REFPIN_OUT_OVERRIDE") == "ara_busy_o@clk_user=0"
    # the single-clock §172 knob would move every port onto one clock
    assert not re.search(r"JANUS_CTS_IO_REFPIN\s*=", MK.read_text())


MOCK = r"""
proc clock_tree_synthesis { args } { return orig }
proc estimate_parasitics { args } {}
proc get_clocks { args } {
  set n [lindex $args end]
  if { $n in {clk_user clk_agent} } { return [list $n] }
  return ""
}
proc get_property { obj prop } {
  if { $prop eq "period" } { return [expr {$obj eq "clk_user" ? 2857.14 : 5000.0}] }
  return $::arr($obj)
}
array set ::arr {u/r1/CLK 3900.0 u/r2/CLK 4020.0 u/r3/CLK 4400.0 a/r1/CLK 1500.0 a/r2/CLK 1700.0 a/r3/CLK 1600.0 m/clk 1100.0}
proc all_registers { args } {
  set c [lindex $args [expr {[lsearch $args -clock] + 1}]]
  if { $c eq "clk_user" } { return {u/r1/CLK u/r2/CLK m/clk u/r3/CLK} }
  return {a/r1/CLK a/r2/CLK a/r3/CLK}
}
proc janus_cts_is_macro_pin { pin } { return [expr {$pin eq "m/clk"}] }
proc get_full_name { o } { return $o }
proc get_ports { args } {
  set p [lindex $args end]
  if { $p eq "gpio_in_i*" } { return {gpio_in_i[0] gpio_in_i[1]} }
  if { $p eq "nope_o" } { return "" }
  return [list $p]
}
foreach c {set_input_delay set_output_delay unset_input_delay unset_output_delay} {
  proc $c { args } "puts \"CMD $c \[join \$args { }\]\""
}
"""


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_hook_refpin_multi_191():
    script = MOCK + f"source {{{HOOK}}}\n" + (
        "proc janus_cts_is_macro_pin { pin } { return [expr {$pin eq \"m/clk\"}] }\n"
        "janus_cts_io_refpin_multi\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("JANUS_CTS_")}
    env.update({
        "JANUS_CTS_IO_REFPIN_MULTI": "1",
        "JANUS_CTS_IO_REFPIN_IN_clk_user": "gpio_in_i* fetch_enable_i",
        "JANUS_CTS_IO_REFPIN_IN_clk_agent": "fetch_enable_i",
        "JANUS_CTS_IO_REFPIN_OUT_clk_user": "gpio_out_o[1] nope_o",
        "JANUS_CTS_IO_REFPIN_OUT_OVERRIDE": "ara_busy_o@clk_user=0 bad",
    })
    out = subprocess.run(["tclsh"], input=script, capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    so = out.stdout
    assert "JANUS_CTS_IO_REFPIN_MULTI: armed" in so
    # clk_user: macro pin excluded, median 4020 -> u/r2/CLK; delay 0.30 * period
    assert "CMD unset_input_delay -clock clk_user {gpio_in_i[0]} {gpio_in_i[1]} fetch_enable_i" in so
    assert re.search(r"CMD set_input_delay -clock clk_user -reference_pin u/r2/CLK -add_delay 857\.14\d* "
                     r"\{gpio_in_i\[0\]\} \{gpio_in_i\[1\]\} fetch_enable_i", so)
    assert re.search(r"CMD set_output_delay -clock clk_user -reference_pin u/r2/CLK -add_delay 857\.14\d* "
                     r"\{gpio_out_o\[1\]\}$", so, re.M)
    assert "CMD set_output_delay -clock clk_user -reference_pin u/r2/CLK -add_delay 0 ara_busy_o" in so
    assert "bad override 'bad'" in so
    # clk_agent: own reference pin (median 1600 -> a/r3/CLK), shared port kept per clock
    assert "CMD unset_input_delay -clock clk_agent fetch_enable_i" in so
    assert "CMD set_input_delay -clock clk_agent -reference_pin a/r3/CLK -add_delay 1500.0 fetch_enable_i" in so
    assert "clk_agent 1 in / 0 out" in so
    # never unsets a clock other than the one being re-referenced
    assert "unset_input_delay -clock clk_agent {gpio" not in so


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_hook_refpin_multi_not_armed_by_default_191():
    script = "proc clock_tree_synthesis { args } { return orig }\n" + f"source {{{HOOK}}}\n"
    env = {k: v for k, v in os.environ.items() if not k.startswith("JANUS_CTS_")}
    out = subprocess.run(["tclsh"], input=script, capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "REFPIN_MULTI: armed" not in out.stdout
