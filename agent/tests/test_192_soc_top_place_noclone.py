"""§192 janus_soc_top: 3_4 resize repair_timing must not clone the clkgen root.

Run 2026-11-02 (§191 in): 3_4 printed `repair_timing -repair_tns 100 -verbose`
(no -skip_gate_cloning) -> "Cloned 8 instances" -> clone_4_u_clkgen/_147_ was the
4_cts hold endpoint (-1417 ps, 14470 hold buffers).
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"
HOOKS = ROOT / "asic_flow/scripts/hooks"
HOOK = HOOKS / "janus_place_repair_noclone.tcl"


def _mkval(key):
    m = re.search(rf"^export\s+{re.escape(key)}\s*=\s*(.*)$", MK.read_text(), re.M)
    assert m, key
    return m.group(1).strip()


def test_mk_disables_orfs_place_repair_and_arms_hook_192():
    assert _mkval("ENABLE_PLACE_REPAIR_TIMING") == "0"
    assert _mkval("JANUS_PLACE_REPAIR_NOCLONE") == "1"
    assert _mkval("SKIP_GATE_CLONING") == "1"  # 2_1 / 4_1 / 5_1 still use the helper
    assert len(re.findall(r"^export\s+ENABLE_PLACE_REPAIR_TIMING", MK.read_text(), re.M)) == 1


def test_post_resize_sources_noclone_before_dump_192():
    txt = (HOOKS / "post_resize.tcl").read_text()
    assert txt.index("janus_place_repair_noclone.tcl]") < txt.index("janus_stage_dump.tcl]")


def test_hook_uses_skip_gate_cloning_and_is_gated_192():
    txt = HOOK.read_text()
    assert "-skip_gate_cloning" in txt
    assert "JANUS_PLACE_REPAIR_NOCLONE" in txt
    assert "u_clkgen" in txt and "JANUS_PRNC: FAIL" in txt
    # never let the hook abort the stage
    assert "catch {repair_timing" in txt


def test_flow_state_restamp_192():
    sys.path.insert(0, str(ROOT / "asic_flow/utils"))
    import flow_state as fs
    assert fs._earliest_stage_for_keys(["ENABLE_PLACE_REPAIR_TIMING"]) == "3_place"
    assert fs._earliest_stage_for_keys(["JANUS_PLACE_REPAIR_NOCLONE"]) == "3_place"


@pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh not installed")
def test_hook_tcl_mock_192(tmp_path):
    """Run the hook in plain tclsh with stubbed OpenROAD commands."""
    drv = tmp_path / "drv.tcl"
    drv.write_text(f"""
set ::calls {{}}
set ::insts {{u_a clone_1_u_x/_5_}}
proc estimate_parasitics {{args}} {{ lappend ::calls "ep $args" }}
proc repair_timing {{args}} {{ lappend ::calls "rt $args"; lappend ::insts clone_9_u_clkgen/_147_ }}
set ::k 0
namespace eval ord {{ proc get_db_block {{}} {{ return ::blk }} }}
proc ::blk {{m}} {{
  set r {{}}
  foreach i $::insts {{
    set h ::inst[incr ::k]
    proc $h {{m}} [list return $i]
    lappend r $h
  }}
  return $r
}}
# off -> no-op
set ::env(JANUS_PLACE_REPAIR_NOCLONE) 0
source {HOOK}
puts "OFF [llength $::calls]"
set ::env(JANUS_PLACE_REPAIR_NOCLONE) 1
set ::env(TNS_END_PERCENT) 100
source {HOOK}
puts "CALLS $::calls"
""")
    out = subprocess.run(["tclsh", str(drv)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "OFF 0" in out.stdout
    assert "-skip_gate_cloning" in out.stdout and "-repair_tns 100" in out.stdout
    assert "JANUS_PRNC: FAIL 1 clkgen clone(s): clone_9_u_clkgen/_147_" in out.stdout
