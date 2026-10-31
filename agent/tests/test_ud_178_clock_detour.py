"""§178: user_domain clock-net straddle detour (run 20261030_180818, 1614 = 2 clock nets)."""
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
UD = ROOT / "asic_flow/configs/user_domain.mk"
UC = ROOT / "asic_flow/configs/user_core.mk"
HOOK = ROOT / "asic_flow/scripts/hooks/janus_macro_detour.tcl"


def test_user_domain_enables_clock_detour():
    mk = UD.read_text()
    assert re.search(r"^export JANUS_MACRO_DETOUR_CLOCK\s*=\s*1\s*$", mk, re.M)
    assert re.search(r"^export JANUS_MACRO_DETOUR_CLK_SEG_UM\s*=\s*80\s*$", mk, re.M)


def test_user_core_untouched():
    assert "JANUS_MACRO_DETOUR_CLOCK" not in UC.read_text()


def test_hook_clock_default_off_and_procs():
    t = HOOK.read_text()
    assert "env JANUS_MACRO_DETOUR_CLOCK 0" in t
    for s in ("proc macro_clk_arr", "proc restore_clk", "proc pad_pin", "proc clk_path",
              "janus_clkpad_", "JANUS_MACRO_DETOUR_CLK_TOL", "JANUS_MACRO_DETOUR_CLK_SEG_UM"):
        assert s in t, s
    # ODB-0373 guard: dont-touch cleared before iterms are (re)connected
    assert t.count("setDoNotTouch 0") >= 2
    # non-clock behaviour preserved: dont-touch signal straddles still skipped
    assert 'if {$tag ne "clock" && [$n isDoNotTouch]}' in t


def test_flow_state_maps_clock_knobs_to_route():
    import sys
    sys.path.insert(0, str(ROOT / "asic_flow/agent"))
    import flow_state as fs
    assert fs._earliest_stage_for_keys(["JANUS_MACRO_DETOUR_CLOCK",
                                        "JANUS_MACRO_DETOUR_CLK_SEG_UM"]) == "5_route"


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_hook_braces_balance():
    r = subprocess.run(["tclsh"], input=f'set f [open "{HOOK}"]; puts [info complete [read $f]]',
                       capture_output=True, text=True)
    assert r.stdout.strip() == "1"


def test_179_no_sta_port_name_and_failsafe():
    """Run 20261030_183916: `[[$p port] name]` threw 'Invalid method' in planning,
    which dropped ALL detours (ovfl back to 27438). Use the ODB iterm and make
    clock-side failures drop only the clock entries."""
    t = HOOK.read_text()
    assert "[$p port] name" not in t
    assert "sta::sta_to_db_pin $p" in t
    assert "signal detours continue" in t
    assert "catch {set clk_before [macro_clk_arr]}" in t
    assert "catch {restore_clk $blk $clk_before $dbu}" in t
