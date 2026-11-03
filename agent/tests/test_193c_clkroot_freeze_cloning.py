"""§193c: freeze only the clock-source cells (upsize + relocate), keep gate cloning
everywhere else (Kiran: cloning is needed for downstream STA closure)."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow/scripts/hooks"
GUARD = HOOKS / "janus_clkroot_guard.tcl"
PRNC = HOOKS / "janus_place_repair_noclone.tcl"
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"


def _mk(key):
    m = re.search(rf"^export\s+{re.escape(key)}\s*=\s*(.*)$", MK.read_text(), re.M)
    return m and m.group(1).strip()


def test_mk_193c():
    for k in ("JANUS_CLKROOT_UPSIZE", "JANUS_CLKROOT_RELOCATE", "JANUS_CLKROOT_ALLOW_CLONING"):
        assert _mk(k) == "1", k
    assert _mk("PRE_GLOBAL_ROUTE_TCL").endswith("hooks/janus_soc_pre_grt.tcl")
    assert (HOOKS / "janus_soc_pre_grt.tcl").exists()
    assert _mk("SKIP_GATE_CLONING") == "1"  # still binds the pre-placement 2_1 call


def test_hook_wiring_193c():
    assert "janus_crg::relocate global_place" in (HOOKS / "post_global_place.tcl").read_text()
    cb = (HOOKS / "janus_cts_balance.tcl").read_text()
    # §194: allow_cloning is reached through the dont_touch window
    assert cb.index("janus_crg::run pre_cts 1") < cb.index("janus_crg::wrap_repair_timing cts")
    pg = (HOOKS / "janus_soc_pre_grt.tcl").read_text()
    assert pg.index("janus_crg::run pre_global_route 1") < pg.index("janus_crg::wrap_repair_timing global_route")
    p = PRNC.read_text()
    assert p.index("janus_crg::run resize_pre") < p.index("janus_crg::window_begin resize")
    assert "allow_cloning $stage" in GUARD.read_text()


def test_flow_state_193c():
    sys.path.insert(0, str(ROOT / "asic_flow/utils"))
    import flow_state as fs
    for k in ("JANUS_CLKROOT_UPSIZE", "JANUS_CLKROOT_RELOCATE", "JANUS_CLKROOT_ALLOW_CLONING"):
        assert fs._earliest_stage_for_keys([k]) == "3_place"


# ---- tclsh mock of the OpenROAD/ODB/STA surface used by the guard ----------
MOCK = r"""
set ::dbu 1000
# name -> {master x y w h isblock}
array set ::I {
  u_clkgen/_146_ {AND2x2_ASAP7_75t_SL 1640000 1373000 300 270 0}
  u_clkgen/_147_ {AND3x1_ASAP7_75t_R  1723000 1399000 300 270 0}
  ff_a          {DFFx1 100000 100000 300 270 0}
  ff_b          {DFFx1 2100000 1300000 300 270 0}
  u_mac         {MACRO 1000000 600000 400000 200000 1}
}
array set ::DT {}
set ::masters {AND2x2_ASAP7_75t_SL AND2x4_ASAP7_75t_SL AND2x6_ASAP7_75t_SL AND2x6_ASAP7_75t_R
               AND3x1_ASAP7_75t_R AND3x2_ASAP7_75t_R AND3x4_ASAP7_75t_R AND3x4_ASAP7_75t_SL DFFx1}
proc all_clocks {} { return {c_ref c_user c_agent} }
proc get_property {o p} {
  if {$p eq "clocks"} {
    switch $o { ff_a/CLK { return c_user } ff_b/CLK { return c_user } default { return {} } }
  }
  switch $o { c_ref { return p_ref } c_user { return p146 } c_agent { return p147 } }
}
proc get_full_name {o} { switch $o { p_ref { return ref_clk_i } p146 { return u_clkgen/_146_/Y } p147 { return u_clkgen/_147_/Y } } }
proc get_name {c} { return [string map {c_ ""} $c] }
proc get_cells {args} { return [lindex $args end] }
proc get_pins {args} { return [lindex $args end] }
proc set_dont_touch {c} { set ::DT($c) 1 }
proc unset_dont_touch {c} { unset -nocomplain ::DT($c) }
namespace eval ord { proc get_db_block {} { return ::blk }; proc get_db {} { return ::db } }
proc ::db {m} { return ::lib }
proc ::lib {m} { set r {}; foreach n $::masters { lappend r [mkmaster $n] }; return $r }
proc mkmaster {n} {
  set h ::m_$n
  proc $h {m args} [string map [list NAME $n] {
    switch $m {
      getName { return NAME } isBlock { return [string equal NAME MACRO] }
      findMTerm { if {[string match DFF* NAME]} { return ::mt_CLK }; return NULL }
      getWidth { return 300 } getHeight { return 270 }
    }
  }]
  return $h
}
proc ::mt_CLK {m args} { return CLK }
proc ::mt_clk {m args} { return clk }
proc mkinst {n} {
  set h ::i_[string map {/ _} $n]
  proc $h {m args} [string map [list NAME $n] {
    lassign $::I(NAME) mst x y w h b
    switch $m {
      getName { return NAME }
      getMaster { return [mkmaster $mst] }
      swapMaster { lset ::I(NAME) 0 [[lindex $args 0] getName]; return 1 }
      isDoNotTouch { return [info exists ::DT(NAME)] }
      setDoNotTouch { if {[lindex $args 0]} { set ::DT(NAME) 1 } else { unset -nocomplain ::DT(NAME) } }
      getLocation { return [list $x $y] }
      setLocation { lset ::I(NAME) 1 [lindex $args 0]; lset ::I(NAME) 2 [lindex $args 1] }
      setPlacementStatus { set ::ST(NAME) [lindex $args 0] }
      getBBox { return [mkbox $x $y [expr {$x+$w}] [expr {$y+$h}]] }
      getITerms { if {[string match DFF* $mst]} { return [list ::it_CLK] }; return {} }
    }
  }]
  return $h
}
proc ::it_CLK {m args} { switch $m { isInputSignal { return 1 } getMTerm { return ::mt_CLK } } }
proc mkbox {a b c d} {
  set h ::bx[incr ::bxn]
  proc $h {m} [list switch \$m [list xMin {return A} yMin {return B} xMax {return C} yMax {return D}]]
  proc $h {m} [string map [list A $a B $b C $c D $d] {switch $m { xMin {return A} yMin {return B} xMax {return C} yMax {return D} }}]
  return $h
}
set ::bxn 0
proc ::blk {m args} {
  switch $m {
    getInsts { set r {}; foreach n [lsort [array names ::I]] { lappend r [mkinst $n] }; return $r }
    findInst { if {[info exists ::I([lindex $args 0])]} { return [mkinst [lindex $args 0]] }; return NULL }
    getDbUnitsPerMicron { return $::dbu }
    getCoreArea { return [mkbox 20000 20000 2440000 1420000] }
  }
}
source @GUARD@
set ::env(JANUS_CLKROOT_GUARD) 1
set ::env(JANUS_CLKROOT_GUARD_FATAL) 1
@BODY@
"""


def _run(tmp_path, body):
    drv = tmp_path / "d.tcl"
    drv.write_text(MOCK.replace("@GUARD@", "{%s}" % GUARD).replace("@BODY@", body))
    out = subprocess.run(["tclsh", str(drv)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


needs_tcl = pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh not installed")


@needs_tcl
def test_upsize_same_vt_capped_193c(tmp_path):
    out = _run(tmp_path, """
set ::env(JANUS_CLKROOT_UPSIZE) 1
janus_crg::run floorplan 0
puts "M146 [lindex $::I(u_clkgen/_146_) 0] M147 [lindex $::I(u_clkgen/_147_) 0]"
""")
    assert "M146 AND2x6_ASAP7_75t_SL M147 AND3x4_ASAP7_75t_R" in out  # VT kept, x<=6


@needs_tcl
def test_allow_cloning_only_when_frozen_193c(tmp_path):
    out = _run(tmp_path, """
set ::env(JANUS_CLKROOT_ALLOW_CLONING) 1
set ::env(SKIP_GATE_CLONING) 1
puts "A0 [janus_crg::allow_cloning pre_cts] [info exists ::env(SKIP_GATE_CLONING)]"
janus_crg::run pre_cts 1
puts "A1 [janus_crg::allow_cloning pre_cts] [info exists ::env(SKIP_GATE_CLONING)]"
puts "W [janus_crg::window_begin cts] [info exists ::env(SKIP_GATE_CLONING)] [array size ::DT]"
janus_crg::window_end cts 1
puts "E [info exists ::env(SKIP_GATE_CLONING)] $::env(SKIP_GATE_CLONING) [array size ::DT]"
""")
    assert "A0 0 1" in out   # not frozen -> keep skip
    assert "A1 0 1" in out   # §194: run no longer freezes -> still kept
    assert "W 1 0 2" in out  # inside window: frozen, cloning on
    assert "E 1 1 0" in out  # after window: skip restored, released


@needs_tcl
def test_relocate_bbox_centre_outside_macro_193c(tmp_path):
    out = _run(tmp_path, """
set ::env(JANUS_CLKROOT_RELOCATE) 1
janus_crg::relocate global_place
puts "LOC [lrange $::I(u_clkgen/_146_) 1 2] $::ST(u_clkgen/_146_)"
""")
    # clk_user sinks ff_a(100,100) ff_b(2100,1300) -> centre (1100,700) is inside
    # u_mac (1000..1400 x 600..800, +5um halo) -> nearest edge x0 = 995 um
    assert "LOC 995000 700000 PLACED" in out
    assert "relocate u_clkgen/_146_ (user, 2 sinks)" in out
