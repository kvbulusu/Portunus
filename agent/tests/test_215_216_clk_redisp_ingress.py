"""§215 / §216 (run 20261105_172042, 4_cts pre-repair S -1003.1 / H -865.3).
§215: legalize after MACRO_ALIGN left one clock stage at 1067.65 ps (late
      capture on all 5 worst hold paths) -> JANUS_CTS_CLK_REDISP re-runs
      repair_clock_nets + detailed_placement when clock cells were displaced.
§216: u_ucut_mn -> axi4_link_slice MODE 3 (pin-side ingress AW/W/AR, egress
      B/R; INGRESS_BYPASS=0, +1 cycle u_user->NoC).
"""
import re
import subprocess
from pathlib import Path

from test_209_pin_fence_dpl import MK

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow/scripts/hooks"
CB = (HOOKS / "janus_cts_balance.tcl").read_text()
CCR = (HOOKS / "janus_cts_clk_redisp.tcl").read_text()
RS = (ROOT / "rtl/noc/axi4_register_slice.sv").read_text()
LS = (ROOT / "rtl/noc/axi4_link_slice.sv").read_text()
TOP = (ROOT / "rtl/top/janus_soc_top.sv").read_text()
RT = (ROOT / "scripts/run_tests.sh").read_text()


def _hier():
    h = MK[MK.index("ifneq ($(JANUS_FLAT),1)"):]
    return h[:h.index("\nendif")]


def test_mk_clk_redisp_on_with_legalize():
    h = _hier()
    assert re.search(r"^export JANUS_CTS_CLK_REDISP\s*=\s*1\s*$", h, re.M)
    assert re.search(r"^export JANUS_CTS_CLK_REDISP_MAXDISP_UM\s*=\s*20\s*$", h, re.M)
    assert re.search(r"^export JANUS_CTS_LEGALIZE\s*=\s*1\s*$", h, re.M)


def test_hook_no_sdc_no_exceptions():
    code = "\n".join(l for l in CCR.splitlines() if not l.lstrip().startswith("#"))
    for bad in ("set_false_path", "set_multicycle_path", "set_max_delay",
                "set_min_delay", "read_sdc", "set_disable_timing", "remove_buffers"):
        assert bad not in code, bad


def test_balance_sources_hook_and_legalize_calls_it():
    i_src = CB.index("janus_cts_clk_redisp.tcl")
    assert i_src < CB.index("janus_cts_tap_split.tcl")
    p = CB[CB.index("proc janus_cts_legalize"):CB.index("# §198b JANUS_CTS_HOLD_ABORT_PS")]
    i_snap = p.index("janus_ccr_snap")
    i_dpl = p.index("catch { detailed_placement")
    i_red = p.index("janus_cts_clk_redisp $tag $a")
    assert i_snap < i_dpl < i_red
    assert "JANUS_CTS_CLK_REDISP: armed" in CB


_STUB = r"""
set ::calls {}
set ::moved 0
proc estimate_parasitics {args} { lappend ::calls est }
proc repair_clock_nets {args} { lappend ::calls [list rcn {*}$args]; set ::moved 0 }
proc detailed_placement {args} { lappend ::calls [list dpl {*}$args] }
proc janus_ofs_wns {} { return "S -1 H -2" }
# --- minimal odb mock: 2 std cells (cb = clock buffer, sg = signal) + 1 macro
namespace eval ord {}
proc ord::get_db_block {} { return BLK }
proc BLK {m args} {
  switch $m {
    getInsts { return {I_cb I_sg I_mac} }
    getTech { return TECH }
    findInst { return "I_[lindex $args 0]" }
  }
}
proc TECH {m} { return 1000 }
proc mkinst {n isblk isclk} {
  proc I_$n {m args} [string map [list @N $n @B $isblk @C $isclk] {
    switch $m {
      getName { return @N }
      getMaster { return M_@B }
      getPlacementStatus { return PLACED }
      getLocation {
        if { $::moved && "@N" eq "cb" } { return {500000 0} }
        if { $::moved && "@N" eq "sg" } { return {0 90000} }
        return {0 0}
      }
      getITerms { return [list IT_@N] }
    }
  }]
  proc IT_$n {m args} [string map [list @C $isclk] {
    switch $m {
      isOutputSignal { return 1 }
      getNet { return NET_@C }
    }
  }]
}
proc M_0 {m} { return 0 }
proc M_1 {m} { return 1 }
proc NET_1 {m} { switch $m { getSigType { return CLOCK } getITerms { return {} } } }
proc NET_0 {m} { switch $m { getSigType { return SIGNAL } getITerms { return {} } } }
mkinst cb 0 1
mkinst sg 0 0
mkinst mac 1 0
"""


def _tcl(body):
    r = subprocess.run(["tclsh"], input=_STUB + CCR + body, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_redisp_off_is_noop():
    out = _tcl(r"""
janus_ccr_snap
set ::moved 1
janus_cts_clk_redisp t0 {-max_displacement {500 500}}
puts "CALLS <$::calls>"
""")
    assert "CALLS <>" in out
    assert "JANUS_CTS_CLK_REDISP" not in out


def test_redisp_on_displaced_clock_cell_repairs_and_relegalizes():
    out = _tcl(r"""
set ::env(JANUS_CTS_CLK_REDISP) 1
set ::env(JANUS_CTS_CLK_REDISP_MAX_WIRE_UM) 120
janus_ccr_snap
puts "SNAP [lsort [array names ::janus_ccr_loc]]"
set ::moved 1
janus_cts_clk_redisp macro_align {-max_displacement {500 500}}
puts "CALLS <$::calls>"
puts "LEFT [array exists ::janus_ccr_loc]"
""")
    assert "SNAP cb sg" in out                     # macro not snapshotted
    assert "macro_align round 0: 2 cell(s) displaced > 20 um (1 on clock nets)" in out
    assert "500.0 um CLK cb (0.0,0.0) -> (500.0,0.0)" in out
    assert "90.0 um sig sg" in out
    assert "rcn -max_wire_length 120" in out
    assert "dpl -max_displacement {500 500}" in out
    assert "round 1 repair_clock_nets+legalize before S -1 H -2 after S -1 H -2" in out
    assert "round 1: 0 cell(s) displaced" in out
    assert "still displaced" not in out
    assert "LEFT 0" in out


def test_redisp_signal_only_displacement_no_repair():
    out = _tcl(r"""
set ::env(JANUS_CTS_CLK_REDISP) 1
janus_ccr_snap
proc I_cb {m args} { switch $m { getName {return cb} getMaster {return M_0}
  getPlacementStatus {return PLACED} getLocation {return {0 0}} getITerms {return {IT_cb}} } }
set ::moved 1
janus_cts_clk_redisp tap_split {}
puts "CALLS <$::calls>"
""")
    assert "1 cell(s) displaced > 20 um (0 on clock nets)" in out
    assert "CALLS <>" in out


def test_redisp_without_snapshot_warns():
    out = _tcl(r"""
set ::env(JANUS_CTS_CLK_REDISP) 1
janus_cts_clk_redisp t9 {}
puts "CALLS <$::calls>"
""")
    assert "WARNING t9 no pre-legalize snapshot; skipped" in out
    assert "CALLS <>" in out


def test_rtl_modes_3_and_4_present():
    assert "g_ingress" in RS and "g_egress" in RS
    assert "MODE inside {0, 1, 2, 3, 4}" in RS
    eg = RS[RS.index("g_egress"):RS.index("g_skid_local")]
    assert re.search(r"mr_q\s*<=\s*m_ready_i;", eg)      # late ready -> flop D only
    assert "assign s_ready_o = rdy_q;" in eg
    assert re.search(r"MODE_MS\s*=\s*\(MODE == 3\) \? 4 : MODE", LS)
    assert "INGRESS_BYPASS" in LS


def test_top_ucut_mn_mode3_others_unchanged():
    assert "axi4_link_slice #(.MODE(3), .INGRESS_BYPASS(1'b0)) u_ucut_mn (" in TOP
    assert "axi4_link_slice #(.MODE(2)) u_ucut_m (" in TOP
    assert "axi4_link_slice u_ucut_s3 (" in TOP
    assert "axi4_link_slice u_ucut_npu (" in TOP


def test_run_tests_and_struct_script():
    for tb in ("tb_216_ingress", "tb_216_ingress_nobyp", "tb_216_egress"):
        assert f"[{tb}]=" in RT
        order = RT[RT.index('ORDER="'):]
        assert f" {tb} " in order.split("\n")[0]
    s = (ROOT / "scripts/check_216_ingress_struct.sh").read_text()
    assert "egress" in s and "MODE 4" in s
    assert (ROOT / "tb/tb_216_egress.sv").exists()
