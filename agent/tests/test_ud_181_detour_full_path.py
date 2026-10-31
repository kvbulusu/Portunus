"""§181: user_domain signal-straddle detour repeated along the WHOLE path.

Run 20261030_211854 (RCX): all 8 setup violators (-352.2 reg2reg, -282.8
reg2out) ran through janus_detour_{0,1,2,4,7}_a, whose dont-touch driver net
had 820-872 ps wire delay / 2.6-2.8 ns slew. JANUS_MACRO_DETOUR_FULL_PATH=1
puts the repeaters on driver -> corner -> corner -> loads (like §178 clocks).
"""
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
UD = ROOT / "asic_flow/configs/user_domain.mk"
UC = ROOT / "asic_flow/configs/user_core.mk"
HOOK = ROOT / "asic_flow/scripts/hooks/janus_macro_detour.tcl"


def test_user_domain_enables_full_path():
    mk = UD.read_text()
    assert re.search(r"^export JANUS_MACRO_DETOUR_FULL_PATH\s*=\s*1\s*$", mk, re.M)
    # segment pitch / buffer stay at the hook defaults (100 um, BUFx4f)
    assert not re.search(r"^export JANUS_MACRO_DETOUR_SEG_UM", mk, re.M)


def test_user_core_untouched():
    assert "JANUS_MACRO_DETOUR_FULL_PATH" not in UC.read_text()


def test_hook_default_off_and_legacy_path_kept():
    t = HOOK.read_text()
    assert "env JANUS_MACRO_DETOUR_FULL_PATH 0" in t
    # full-path entries carry driver/load xy; legacy entries keep 5 fields
    assert "lappend out [list $n straddle $pd $pl $move $dxy $lxy $ok_h]" in t
    assert "lappend out [list $n straddle $pd $pl $move]" in t
    # whole-path geometry reuses the §178 helpers, min 2 buffers (a + b)
    assert "set pts [clk_path $dxy $pd $pl $lxy $okh]" in t
    assert "set nseg [expr {max(3, int(ceil(double($len) / $seg)))}]" in t
    # legacy corner-leg branch still present
    assert "set nseg [expr {max(1, int(ceil(double($len) / $seg)))}]" in t
    # dont-touch policy unchanged: driver net + legs dont-touch, n2 repairable
    assert "$n setDoNotTouch 1\n      foreach l $legs { $l setDoNotTouch 1 }" in t


def test_flow_state_maps_knob_to_route():
    sys.path.insert(0, str(ROOT / "asic_flow/agent"))
    import flow_state as fs
    assert fs._earliest_stage_for_keys(["JANUS_MACRO_DETOUR_FULL_PATH"]) == "5_route"


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_hook_braces_balance():
    r = subprocess.run(["tclsh"], input=f'set f [open "{HOOK}"]; puts [info complete [read $f]]',
                       capture_output=True, text=True)
    assert r.stdout.strip() == "1"
