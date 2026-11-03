"""§193b clock-root dont_touch + clone guard (janus_soc_top).

soc_cts_probe on run 2026-11-02 4_cts: 7 clone_N_u_clkgen/_146_ carried ref_clk
(12522 sinks) and 5 clone_N_u_clkgen/_147_ carried clk_user into the agent tree.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow/scripts/hooks"
GUARD = HOOKS / "janus_clkroot_guard.tcl"
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"


def test_mk_arms_guard_193b():
    txt = MK.read_text()
    assert re.search(r"^export\s+JANUS_CLKROOT_GUARD\s*=\s*1\s*$", txt, re.M)
    assert re.search(r"^export\s+JANUS_CLKROOT_GUARD_FATAL\s*=\s*1\s*$", txt, re.M)


@pytest.mark.parametrize("hook,stage,fatal", [
    ("post_floorplan.tcl", "floorplan", "0"),
    ("janus_cts_balance.tcl", "pre_cts", "1"),
    ("post_cts.tcl", "cts", "1"),
    ("post_global_route.tcl", "global_route", "0"),
    ("janus_place_repair_noclone.tcl", "resize", "1"),
])
def test_hooks_call_guard_193b(hook, stage, fatal):
    txt = (HOOKS / hook).read_text()
    assert "janus_clkroot_guard.tcl]" in txt
    assert f"janus_crg::run {stage} {fatal}" in txt


def test_guard_runs_before_dump_193b():
    for h in ("post_floorplan.tcl", "post_cts.tcl", "post_global_route.tcl"):
        t = (HOOKS / h).read_text()
        assert t.index("janus_crg::run") < t.index("janus_stage_dump.tcl]"), h


def test_flow_state_193b():
    sys.path.insert(0, str(ROOT / "asic_flow/utils"))
    import flow_state as fs
    assert fs._earliest_stage_for_keys(["JANUS_CLKROOT_GUARD"]) == "3_place"
    assert fs._earliest_stage_for_keys(["JANUS_CLKROOT_GUARD_FATAL"]) == "3_place"


MOCK = r"""
set ::dt {}
set ::odbdt {}
set ::insts {u_clkgen/_146_ u_clkgen/_147_ u_a/r_q INSTS_EXTRA}
proc all_clocks {} { return {c_ref c_user c_agent} }
proc get_property {o p} {
  switch $o {
    c_ref   { return {p_refport} }
    c_user  { return {p_146} }
    c_agent { return {p_147} }
  }
}
proc get_full_name {o} {
  switch $o { p_refport { return ref_clk_i } p_146 { return u_clkgen/_146_/Y } p_147 { return u_clkgen/_147_/Y } }
}
proc get_cells {args} { return [lindex $args end] }
proc set_dont_touch {c} { lappend ::dt $c }
namespace eval ord { proc get_db_block {} { return ::blk } }
set ::k 0
proc mkinst {n} {
  set h ::inst[incr ::k]
  proc $h {m args} [string map [list NAME $n] {
    switch $m { getName { return NAME } isDoNotTouch { return [expr {[lsearch $::dt NAME] >= 0}] } setDoNotTouch { lappend ::odbdt NAME } }
  }]
  return $h
}
proc ::blk {m args} {
  switch $m {
    getInsts { set r {}; foreach i $::insts { lappend r [mkinst $i] }; return $r }
    findInst { if {[lsearch -exact $::insts [lindex $args 0]] >= 0} { return [mkinst [lindex $args 0]] }; return NULL }
  }
}
source @GUARD@
set ::env(JANUS_CLKROOT_GUARD) 0
janus_crg::run off 1
puts "OFF [llength $::dt]"
set ::env(JANUS_CLKROOT_GUARD) 1
set ::env(JANUS_CLKROOT_GUARD_FATAL) 1
janus_crg::run s0 0
puts "DT $::dt"
set rc [catch {janus_crg::run s1 1} e]
puts "RC $rc"
"""


def _run(tmp_path, extra):
    drv = tmp_path / "d.tcl"
    drv.write_text(MOCK.replace("@GUARD@", "{%s}" % GUARD).replace("INSTS_EXTRA", extra))
    return subprocess.run(["tclsh", str(drv)], capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh not installed")
def test_guard_mock_clean_193b(tmp_path):
    out = _run(tmp_path, "clone_1_u_a/_9_")
    assert out.returncode == 0, out.stderr
    assert "OFF 0" in out.stdout
    assert "DT u_clkgen/_146_ u_clkgen/_147_" in out.stdout  # port source skipped
    assert "JANUS_CRG: OK s1" in out.stdout and "RC 0" in out.stdout
    assert "u_clkgen/_146_=1" in out.stdout


@pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh not installed")
def test_guard_mock_clone_fatal_193b(tmp_path):
    out = _run(tmp_path, "clone_4_u_clkgen/_147_ clone_1_u_clkgen/_146_")
    assert out.returncode == 0, out.stderr
    # non-fatal call reports, fatal call errors
    assert "JANUS_CRG: FAIL s0 2 clock-root clone(s)" in out.stdout
    assert "RC 1" in out.stdout
